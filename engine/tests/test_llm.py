"""The advisor must be impossible to depend on.

The brief requires the system to stay predictable when optional models fail, so
the tests here are mostly about failure: every broken model must produce the
same decisions as no model at all.
"""

import pytest

from leash.config import Settings
from leash.decision import DecisionEngine, RunState
from leash.llm import Advice, IntentAdvisor
from leash.models.enums import Decision
from leash.policy import compile_policy
from leash.replay import EventBuilder

ENABLED = Settings(
    base_url="https://sandbox.invalid",
    api_key=None,
    decision_budget_ms=2500,
    llm_enabled=True,
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
    late = Settings(ENABLED.base_url, None, 2500, True, "m", 10)

    def slow(system, user, *, timeout_s):
        import time

        time.sleep(0.05)
        return '{"verdict":"mismatch"}'

    assert IntentAdvisor(late, slow).compare("a", "b", "c") is None


def test_the_advisor_is_off_unless_explicitly_enabled():
    off = Settings(ENABLED.base_url, None, 2500, False, "m", 900)
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
