"""The advisor must be impossible to depend on.

The brief requires the system to stay predictable when optional models fail, so
the tests here are mostly about failure: every broken model must produce the
same decisions as no model at all.
"""

import subprocess
import sys

import pytest

from leash.config import DEFAULT_LLM_MODEL, Settings
from leash.decision import DecisionEngine, RunState
from leash.llm import Advice, IntentAdvisor
from leash.llm.advisor import _openrouter_completion
from leash.models.enums import Decision
from leash.policy import compile_policy
from leash.replay import EventBuilder

ENABLED = Settings(
    base_url="https://sandbox.invalid",
    api_key=None,
    decision_budget_ms=2500,
    llm_enabled=True,
    llm_provider="anthropic",
    llm_model="claude-haiku-4-5-20251001",
    llm_timeout_ms=900,
)


def advisor_replying(text: str, *, delay_ms: float = 0.0) -> IntentAdvisor:
    def completion(system, user, *, timeout_s):
        if delay_ms:
            import time

            time.sleep(delay_ms / 1000)
        return text

    return IntentAdvisor(ENABLED, completion)


def advisor_raising(exc: Exception) -> IntentAdvisor:
    def completion(system, user, *, timeout_s):
        raise exc

    return IntentAdvisor(ENABLED, completion)


def test_llm_package_can_be_imported_before_the_decision_package():
    result = subprocess.run(
        [sys.executable, "-c", "from leash.llm import IntentAdvisor, ClarificationPlanner"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_openrouter_transport_uses_the_gemini_model_and_bearer_key(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"verdict":"match"}'}}]}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr("httpx.post", post)
    raw = _openrouter_completion(DEFAULT_LLM_MODEL, "secret")(
        "system", "user", timeout_s=1.2
    )

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["model"] == "google/gemini-2.5-flash-lite"
    assert captured["json"]["response_format"] == {"type": "json_object"}
    assert raw == '{"verdict":"match"}'


@pytest.fixture(scope="module")
def builder() -> EventBuilder:
    return EventBuilder()


def replay(builder, scenario_id, advisor=None):
    policy = compile_policy(builder.catalogue[scenario_id]["cardholder_instruction"])
    events = builder.build(scenario_id, policy).events
    engine, state = DecisionEngine(advisor=advisor), RunState()
    return {e.authorization.source_authorization_id: engine.decide(e, state) for e in events}


# --- parsing the reply -----------------------------------------------------


def test_a_well_formed_reply_is_used():
    result = advisor_replying('{"verdict":"mismatch","why":"a trail shoe, not a road shoe"}').compare(
        "road-running shoes", "Trail-running shoes", "sporting_goods"
    )
    assert result.advice is Advice.MISMATCH
    assert "trail" in result.why


def test_prose_around_the_json_is_tolerated():
    result = advisor_replying('Sure! {"verdict":"match","why":"same item"} Hope that helps.').compare(
        "27-inch monitor", "27-inch computer monitor", "electronics"
    )
    assert result.advice is Advice.MATCH


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "not json at all",
        "{}",
        '{"verdict":"approve"}',            # not one of our three answers
        '{"verdict":null}',
        "[1, 2, 3]",
        '{"verdict":"match"',               # truncated
    ],
)
def test_an_unusable_reply_is_dropped(reply):
    assert advisor_replying(reply).compare("a", "b", "c") is None


def test_case_is_normalised_but_the_shape_is_not_negotiable():
    assert advisor_replying('{"verdict":"MATCH"}').compare("a", "b", "c").advice is Advice.MATCH


@pytest.mark.parametrize(
    "exc",
    [TimeoutError("slow"), ConnectionError("down"), RuntimeError("boom"), ValueError("odd")],
)
def test_any_failure_returns_nothing(exc):
    assert advisor_raising(exc).compare("a", "b", "c") is None


def test_a_reply_that_arrives_too_late_is_not_used():
    """The deadline is not ours to spend on a second opinion."""
    late = Settings(ENABLED.base_url, None, 2500, True, "m", 10, "anthropic")

    def slow(system, user, *, timeout_s):
        import time

        time.sleep(0.05)
        return '{"verdict":"mismatch"}'

    assert IntentAdvisor(late, slow).compare("a", "b", "c") is None


def test_the_advisor_is_off_unless_explicitly_enabled():
    off = Settings(ENABLED.base_url, None, 2500, False, "m", 900, "anthropic")
    assert not IntentAdvisor(off, lambda *a, **k: '{"verdict":"match"}').available


# --- it can never make the engine more permissive -------------------------


def test_decisions_are_identical_with_no_model(builder):
    for scenario_id in builder.scenario_ids():
        without = {k: v.decision for k, v in replay(builder, scenario_id).items()}
        disabled = {
            k: v.decision
            for k, v in replay(builder, scenario_id, IntentAdvisor(ENABLED, None)).items()
        }
        assert without == disabled, scenario_id


@pytest.mark.parametrize(
    "broken",
    [
        advisor_raising(TimeoutError("slow")),
        advisor_replying("garbage"),
        advisor_replying('{"verdict":"unsure"}'),
    ],
)
def test_a_broken_model_never_changes_an_approval_into_something_worse_or_better(builder, broken):
    """Every failure mode must leave the deterministic answer standing."""
    baseline = {k: v.decision for k, v in replay(builder, "SCEN0002").items()}
    with_broken = {k: v.decision for k, v in replay(builder, "SCEN0002", broken).items()}
    if broken.compare("a", "b", "c") is None:
        assert with_broken == baseline


def test_a_model_claiming_a_match_cannot_approve_anything(builder):
    """The advisor is one-directional: it can only raise doubt."""
    always_match = advisor_replying('{"verdict":"match","why":"looks right"}')
    baseline = {k: v.decision for k, v in replay(builder, "SCEN0002").items()}
    with_model = {k: v.decision for k, v in replay(builder, "SCEN0002", always_match).items()}
    assert with_model == baseline

    declined = [k for k, d in baseline.items() if d is Decision.DECLINE]
    assert declined, "the scenario must contain declines for this to mean anything"
    for key in declined:
        assert with_model[key] is Decision.DECLINE


def test_a_dissenting_model_can_only_escalate(builder):
    """It raises doubt on an otherwise-clean purchase; it never declines one."""
    dissenter = advisor_replying('{"verdict":"mismatch","why":"not the same product"}')
    baseline = {k: v.decision for k, v in replay(builder, "SCEN0002").items()}
    with_model = replay(builder, "SCEN0002", dissenter)

    for key, before in baseline.items():
        after = with_model[key].decision
        if before is Decision.APPROVE:
            assert after in (Decision.APPROVE, Decision.STEP_UP)
        else:
            assert after is before, f"{key}: a dissent must not change a settled answer"

    escalated = [k for k, v in with_model.items() if v.decision is Decision.STEP_UP]
    assert any(
        "intent_match_disputed" in {f.code for f in with_model[k].findings} for k in escalated
    )


# --- providers -------------------------------------------------------------


def _capture(provider: str, model: str, key_env: str, monkeypatch):
    """Run one compare against a fake transport and return the request made."""
    import httpx

    from leash.llm.advisor import IntentAdvisor as Advisor

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = _json.loads(request.content)
        if provider == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": '{"verdict":"match"}'}]})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"verdict":"match"}'}}]}
        )

    transport = httpx.MockTransport(handler)
    real_post = httpx.post
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    monkeypatch.setenv(key_env, "test-key-123")

    settings = Settings(
        base_url="https://sandbox.invalid",
        api_key=None,
        decision_budget_ms=2500,
        llm_enabled=True,
        llm_provider=provider,
        llm_model=model,
        llm_timeout_ms=5000,
    )
    result = Advisor(settings).compare("27-inch monitor", "27-inch computer monitor", "electronics")
    monkeypatch.setattr(httpx, "post", real_post)
    return result, seen


def test_openrouter_is_called_with_the_chat_shape(monkeypatch):
    result, seen = _capture("openrouter", "openai/gpt-4o-mini", "OPENROUTER_API_KEY", monkeypatch)
    assert result.advice is Advice.MATCH
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer test-key-123"
    assert seen["body"]["model"] == "openai/gpt-4o-mini"
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]
    # OpenRouter asks callers to identify themselves.
    assert "x-title" in seen["headers"]


def test_openai_is_called_with_the_same_shape_at_its_own_endpoint(monkeypatch):
    result, seen = _capture("openai", "gpt-4o-mini", "OPENAI_API_KEY", monkeypatch)
    assert result.advice is Advice.MATCH
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer test-key-123"
    assert seen["body"]["model"] == "gpt-4o-mini"


def test_anthropic_is_called_with_the_messages_shape(monkeypatch):
    result, seen = _capture("anthropic", "claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY", monkeypatch)
    assert result.advice is Advice.MATCH
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "test-key-123"
    assert "system" in seen["body"], "the system prompt is a top-level field here"


def test_a_provider_we_do_not_know_is_simply_unavailable():
    settings = Settings(
        base_url="https://sandbox.invalid", api_key=None, decision_budget_ms=2500,
        llm_enabled=True, llm_provider="some-new-vendor", llm_model="x", llm_timeout_ms=900,
    )
    assert not IntentAdvisor(settings).available


def test_a_provider_with_no_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings(
        base_url="https://sandbox.invalid", api_key=None, decision_budget_ms=2500,
        llm_enabled=True, llm_provider="openrouter", llm_model="x", llm_timeout_ms=900,
    )
    assert not IntentAdvisor(settings).available


def test_one_budget_covers_the_whole_basket_not_each_line(builder):
    """A three-line basket asking three times could spend three timeouts and
    eat the decision deadline."""
    from leash.decision import DecisionEngine, RunState

    calls = {"n": 0}

    def slow_but_agreeable(system, user, *, timeout_s):
        calls["n"] += 1
        import time

        time.sleep(0.05)
        return '{"verdict":"match","why":"same"}'

    tight = Settings(
        base_url="https://sandbox.invalid", api_key=None, decision_budget_ms=2500,
        llm_enabled=True, llm_provider="openrouter", llm_model="x", llm_timeout_ms=60,
    )
    advisor = IntentAdvisor(tight, slow_but_agreeable)
    policy = compile_policy(builder.catalogue["SCEN0002"]["cardholder_instruction"])
    events = builder.build("SCEN0002", policy).events
    engine, state = DecisionEngine(advisor=advisor), RunState()
    for event in events:
        engine.decide(event, state)

    assert calls["n"] <= len(events), "at most one call per event once the budget is spent"
