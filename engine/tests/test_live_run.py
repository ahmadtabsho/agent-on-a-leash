"""The one-command live sequence, against a scripted sandbox.

This is the path that runs in front of judges, so the ordering guarantees are
what matter: the policy is stored before it is confirmed, the worker exists
before the run starts, and a paused purchase is answered through /resolve.
"""

import json

import httpx
import pytest

from leash.api import ApiError, LeashClient
from leash.config import Settings
from leash.models.enums import Decision
from leash.policy import compile_policy
from leash.replay import EventBuilder
from leash.worker import execute, prepare, resolve_pending

SETTINGS = Settings(
    base_url="https://sandbox.invalid",
    api_key="test-key",
    decision_budget_ms=2500,
    llm_enabled=False,
    llm_model="",
    llm_timeout_ms=900,
)


@pytest.fixture(scope="module")
def builder() -> EventBuilder:
    return EventBuilder()


class Sandbox:
    """Records the order it was called in, which is the thing under test."""

    def __init__(self, builder, scenario_id):
        policy = compile_policy(builder.catalogue[scenario_id]["cardholder_instruction"])
        replay = builder.build(scenario_id, policy)
        self.queue = [
            {
                "run_id": "run_live_1",
                "event_id": f"evt_{i}",
                "type": "authorization.request",
                "authorization_id": e.authorization.authorization_id,
                "status": "queued",
                "occurred_at": e.authorization.timestamp.isoformat().replace("+00:00", "Z"),
                "data": e.model_dump(mode="json"),
            }
            for i, e in enumerate(replay.events)
        ]
        self.calls: list[str] = []
        self.drafts: list[dict] = []
        self.decisions: list[dict] = []
        self.resolutions: list[dict] = []
        self.run_started = False
        self.revoked = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if path == "/v1/mandates" and request.method == "POST":
            self.calls.append("create_mandate")
            self.drafts.append(body)
            return httpx.Response(200, json={"draft_id": "DRAFT-1", **body})

        if path.endswith("/confirm"):
            self.calls.append("confirm_mandate")
            return httpx.Response(200, json={"mandate_id": "TM-1", "status": "active"})

        if path == "/v1/scenario-runs":
            self.calls.append("start_run")
            self.run_started = True
            return httpx.Response(200, json={"run_id": "run_live_1", **body})

        if path == "/v1/decision-requests/next":
            self.calls.append("poll")
            if not self.run_started:
                # Nothing may be queued before the run starts.
                return httpx.Response(204)
            if self.queue:
                return httpx.Response(200, json=self.queue.pop(0))
            return httpx.Response(204)

        if path.endswith("/decision"):
            self.calls.append("decision")
            self.decisions.append(body)
            return httpx.Response(200, json={"recorded": True})

        if path.endswith("/resolve"):
            self.calls.append("resolve")
            self.resolutions.append(body)
            return httpx.Response(200, json={"recorded": True})

        if path.startswith("/v1/scenario-runs/"):
            return httpx.Response(200, json={"run_id": "run_live_1", "events_total": 11})

        if request.method == "DELETE" and path.startswith("/v1/mandates/"):
            self.calls.append("revoke")
            self.revoked = True
            return httpx.Response(200, json={"status": "revoked"})

        return httpx.Response(404, json={"error": f"no route for {path}"})


def client_for(sandbox) -> LeashClient:
    transport = httpx.MockTransport(sandbox.handler)
    return LeashClient(SETTINGS, httpx.Client(transport=transport, base_url=SETTINGS.base_url))


def run(builder, scenario_id):
    sandbox = Sandbox(builder, scenario_id)
    live = execute(prepare(scenario_id), client_for(sandbox), wait=0, max_empty_polls=1)
    return sandbox, live


# --- preparation happens offline ------------------------------------------


def test_preparing_touches_no_network(builder):
    """The policy can be reviewed before anything is authorised."""
    live = prepare("SCEN0004")
    assert live.policy.hard_rules
    assert live.draft_id is None and live.mandate_id is None and live.run_id is None


def test_an_unknown_scenario_is_refused_before_any_call():
    with pytest.raises(KeyError):
        prepare("SCEN9999")


def test_a_custom_instruction_overrides_the_catalogue():
    live = prepare("SCEN0000", "Buy me a keyboard for up to CHF 90. Ask me when uncertain.")
    assert "keyboard" in live.instruction
    limits = [r for r in live.policy.hard_rules if r.field == "authorization.billing_amount_chf"]
    assert limits[0].value == 90.0


# --- ordering -------------------------------------------------------------


def test_the_sequence_runs_in_the_required_order(builder):
    sandbox, _ = run(builder, "SCEN0000")
    order = [c for c in sandbox.calls if c in {"create_mandate", "confirm_mandate", "start_run", "poll"}]

    assert order[0] == "create_mandate", "the policy is stored first"
    assert order[1] == "confirm_mandate", "and confirmed before anything runs"
    assert order[2] == "start_run"
    assert "poll" in order[3:], "polling follows the run"


def test_the_run_is_started_only_after_the_mandate_is_confirmed(builder):
    sandbox, _ = run(builder, "SCEN0000")
    assert sandbox.calls.index("confirm_mandate") < sandbox.calls.index("start_run")


def test_the_draft_carries_no_platform_assigned_ids(builder):
    """The platform assigns customer, card and profile when the run starts."""
    sandbox, _ = run(builder, "SCEN0000")
    draft = sandbox.drafts[0]
    assert set(draft) == {
        "instruction",
        "hard_rules",
        "uncertainty_policy",
        "guidance",
        "open_questions",
    }


def test_the_instruction_is_sent_in_the_customers_exact_wording(builder):
    sandbox, live = run(builder, "SCEN0000")
    assert sandbox.drafts[0]["instruction"] == builder.catalogue["SCEN0000"]["cardholder_instruction"]
    assert sandbox.drafts[0]["instruction"] == live.instruction


def test_the_returned_ids_are_captured(builder):
    _, live = run(builder, "SCEN0000")
    assert (live.draft_id, live.mandate_id, live.run_id) == ("DRAFT-1", "TM-1", "run_live_1")


# --- the run --------------------------------------------------------------


def test_every_purchase_is_answered(builder):
    sandbox, live = run(builder, "SCEN0004")
    assert live.stats.decided == 11
    assert len(sandbox.decisions) == 11
    assert all(d["decision"] in {"approve", "decline", "step_up"} for d in sandbox.decisions)


def test_paused_purchases_are_handed_back_for_a_person(builder):
    _, live = run(builder, "SCEN0004")
    assert live.pending
    assert all(p.customer_message for p in live.pending)


def test_the_customers_answer_goes_through_resolve(builder):
    sandbox, live = run(builder, "SCEN0004")
    before = len(sandbox.decisions)

    target = live.pending[0]
    resolve_pending(live, target.authorization_id, Decision.APPROVE, "I meant to order two.")

    assert len(sandbox.resolutions) == 1
    assert sandbox.resolutions[0]["decision"] == "approve"
    assert len(sandbox.decisions) == before, "never a second automated decision after step_up"
    assert target.authorization_id not in {p.authorization_id for p in live.pending}


def test_revoking_reaches_the_platform(builder):
    sandbox, live = run(builder, "SCEN0000")
    client_for(sandbox).revoke_mandate(live.mandate_id)
    assert sandbox.revoked


def test_the_journal_is_written_when_asked(builder, tmp_path):
    sandbox = Sandbox(builder, "SCEN0004")
    path = tmp_path / "live.jsonl"
    execute(prepare("SCEN0004"), client_for(sandbox), log_path=path, wait=0, max_empty_polls=1)

    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 11
    assert all(entry["evidence"] for entry in lines)


# --- failure --------------------------------------------------------------


def test_a_mandate_response_without_a_draft_id_stops_the_run(builder):
    sandbox = Sandbox(builder, "SCEN0000")

    def handler(request):
        if request.url.path == "/v1/mandates":
            return httpx.Response(200, json={"unexpected": "shape"})
        return sandbox.handler(request)

    sandbox.handler_original = sandbox.handler
    client = LeashClient(SETTINGS, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ApiError) as excinfo:
        execute(prepare("SCEN0000"), client, wait=0, max_empty_polls=1)
    assert "draft_id" in str(excinfo.value)
    assert not sandbox.run_started, "no run may start without a confirmed mandate"


def test_a_rejected_key_stops_before_anything_is_stored():
    def handler(request):
        return httpx.Response(401, json={"error": {"code": "unauthorized"}})

    client = LeashClient(SETTINGS, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ApiError) as excinfo:
        execute(prepare("SCEN0000"), client, wait=0, max_empty_polls=1)
    assert excinfo.value.status == 401
