"""The worker, driven against a scripted sandbox.

No team key exists until the event day, so the transport is mocked. What is
being tested is the protocol behaviour the guide warns about, not httpx.
"""

import json

import httpx
import pytest

from leash.api import ApiError, LeashClient
from leash.config import Settings
from leash.decision import DecisionEngine, RunState
from leash.models.enums import Decision
from leash.policy import compile_policy
from leash.replay import EventBuilder
from leash.worker import Worker

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


def envelopes_for(builder, scenario_id):
    policy = compile_policy(builder.catalogue[scenario_id]["cardholder_instruction"])
    replay = builder.build(scenario_id, policy)
    return [
        {
            "run_id": "run_test",
            "event_id": f"evt_{i}",
            "type": "authorization.request",
            "authorization_id": event.authorization.authorization_id,
            "status": "queued",
            "occurred_at": event.authorization.timestamp.isoformat().replace("+00:00", "Z"),
            "data": event.model_dump(mode="json"),
        }
        for i, event in enumerate(replay.events)
    ]


class Sandbox:
    """A scripted stand-in: hands out queued envelopes, then 204s."""

    def __init__(self, queue, *, decision_status=200):
        self.queue = list(queue)
        self.decision_status = decision_status
        self.decisions: list[dict] = []
        self.resolutions: list[dict] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/decision-requests/next":
            self.polls += 1
            if self.queue:
                return httpx.Response(200, json=self.queue.pop(0))
            return httpx.Response(204)
        if path.endswith("/decision"):
            self.decisions.append(json.loads(request.content))
            return httpx.Response(self.decision_status, json={"recorded": True})
        if path.endswith("/resolve"):
            self.resolutions.append(json.loads(request.content))
            return httpx.Response(200, json={"recorded": True})
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={"error": f"no route for {path}"})


def worker_for(sandbox, **kwargs):
    transport = httpx.MockTransport(sandbox.handler)
    client = LeashClient(SETTINGS, httpx.Client(transport=transport, base_url=SETTINGS.base_url))
    return Worker(client, DecisionEngine(), RunState(), settings=SETTINGS, **kwargs)


# --- the loop --------------------------------------------------------------


def test_the_worker_answers_every_delivered_purchase(builder):
    queue = envelopes_for(builder, "SCEN0004")
    sandbox = Sandbox(queue)
    stats = worker_for(sandbox).run_until_idle(wait=0, max_empty_polls=1)

    assert stats.decided == len(queue) == 11
    assert len(sandbox.decisions) == 11
    assert sum(stats.by_decision.values()) == 11


def test_an_empty_poll_is_not_the_end_of_the_run(builder):
    """A 204 means no work in that window. The loop must keep polling."""

    queue = envelopes_for(builder, "SCEN0001")
    sandbox = Sandbox(queue)

    real_handler = sandbox.handler
    gaps = {"left": 2}

    def handler(request):
        if request.url.path == "/v1/decision-requests/next" and gaps["left"]:
            gaps["left"] -= 1
            sandbox.polls += 1
            return httpx.Response(204)
        return real_handler(request)

    sandbox.handler = handler
    stats = worker_for(sandbox).run_until_idle(wait=0, max_empty_polls=3)

    assert stats.decided == 10, "work after an empty window must still be answered"
    assert stats.empty_polls >= 2


def test_every_submitted_decision_matches_the_documented_body(builder):
    sandbox = Sandbox(envelopes_for(builder, "SCEN0002"))
    worker_for(sandbox).run_until_idle(wait=0, max_empty_polls=1)

    for payload in sandbox.decisions:
        assert set(payload) <= {
            "authorization_id",
            "decision",
            "reason_codes",
            "customer_message",
            "evidence",
            "engine_version",
        }
        assert payload["decision"] in {"approve", "decline", "step_up"}
        assert payload["authorization_id"]


def test_a_repeated_delivery_re_sends_the_same_answer(builder):
    """A retry must not charge a spending limit twice."""
    queue = envelopes_for(builder, "SCEN0001")
    sandbox = Sandbox(queue[:3] + queue[:3])
    worker = worker_for(sandbox)
    stats = worker.run_until_idle(wait=0, max_empty_polls=1)

    assert stats.decided == 6
    assert stats.replayed == 3
    assert len(worker.state.records) == 3

    first_three = [d["decision"] for d in sandbox.decisions[:3]]
    second_three = [d["decision"] for d in sandbox.decisions[3:]]
    assert first_three == second_three


def test_an_unreadable_event_is_escalated_not_guessed(builder):
    bad = envelopes_for(builder, "SCEN0000")[0]
    bad["data"]["authorization"]["billing_amount_chf"] = "twenty"
    sandbox = Sandbox([bad])
    stats = worker_for(sandbox).run_until_idle(wait=0, max_empty_polls=1)

    assert stats.parse_failures == 1
    assert sandbox.decisions[0]["decision"] == "step_up"
    assert "event_not_understood" in sandbox.decisions[0]["reason_codes"]


def test_a_paused_purchase_is_held_for_the_customer(builder):
    sandbox = Sandbox(envelopes_for(builder, "SCEN0004"))
    worker = worker_for(sandbox)
    worker.run_until_idle(wait=0, max_empty_polls=1)

    assert worker.pending, "step_up purchases must be queued for a person"
    for review in worker.pending.values():
        assert review.customer_message and review.evidence


def test_a_customer_answer_goes_through_resolve_not_a_second_decision(builder):
    sandbox = Sandbox(envelopes_for(builder, "SCEN0004"))
    worker = worker_for(sandbox)
    worker.run_until_idle(wait=0, max_empty_polls=1)
    before = len(sandbox.decisions)

    authorization_id = next(iter(worker.pending))
    worker.resolve(authorization_id, Decision.APPROVE)

    assert len(sandbox.resolutions) == 1
    assert sandbox.resolutions[0]["decision"] == "approve"
    assert len(sandbox.decisions) == before, "no second automated decision after step_up"
    assert authorization_id not in worker.pending


def test_a_resolved_purchase_becomes_spend(builder):
    sandbox = Sandbox(envelopes_for(builder, "SCEN0004"))
    worker = worker_for(sandbox)
    worker.run_until_idle(wait=0, max_empty_polls=1)

    authorization_id = next(iter(worker.pending))
    record = worker.state.answered(authorization_id)
    assert record.awaiting_customer

    worker.resolve(authorization_id, Decision.APPROVE)
    assert not worker.state.answered(authorization_id).awaiting_customer


def test_the_customer_can_only_approve_or_decline(builder):
    worker = worker_for(Sandbox([]))
    with pytest.raises(ValueError):
        worker.resolve("AUTH1", Decision.STEP_UP)


def test_the_worker_stays_far_inside_the_decision_deadline(builder):
    sandbox = Sandbox(envelopes_for(builder, "SCEN0003"))
    stats = worker_for(sandbox).run_until_idle(wait=0, max_empty_polls=1)
    assert stats.missed_deadlines == 0
    assert stats.slowest_ms < 100


# --- the client ------------------------------------------------------------


def test_an_error_status_is_raised_with_the_servers_message():
    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})

    client = LeashClient(SETTINGS, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ApiError) as excinfo:
        client.bootstrap()
    assert excinfo.value.status == 429
    assert "rate limited" in str(excinfo.value)


def test_a_mandate_draft_never_carries_platform_assigned_ids():
    """The platform assigns customer, card and profile when the run starts."""
    client = LeashClient(SETTINGS, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    with pytest.raises(ApiError):
        client.create_mandate({"instruction": "x", "customer_id": "CU0001"})


def test_a_keyed_call_without_a_key_fails_before_the_network():
    keyless = Settings(SETTINGS.base_url, None, 2500, False, "", 900)
    client = LeashClient(keyless, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(ApiError) as excinfo:
        client.bootstrap()
    assert "TEAM_API_KEY" in str(excinfo.value)


def test_a_real_live_envelope_parses():
    """Captured from the live service, not written from the spec.

    The written contract does not give `event_id` a type; the service sends an
    integer. Typing it as a string rejected every purchase in the first live
    run, which no amount of testing against our own fixtures would have caught.
    """
    import json
    from pathlib import Path

    from leash.models import parse_envelope

    path = Path(__file__).parent / "fixtures_live_envelope.json"
    envelope = parse_envelope(json.loads(path.read_text()))

    assert isinstance(envelope.event_id, int)
    assert envelope.authorization_id.startswith("LA_")
    assert envelope.data.authorization.source_authorization_id == "AU0001"
    assert not envelope.is_redelivery


def test_a_redelivered_envelope_is_recognisable():
    """The service redelivers after 3 seconds; delivery_count says so."""
    import json
    from pathlib import Path

    from leash.models import parse_envelope

    path = Path(__file__).parent / "fixtures_live_envelope.json"
    raw = json.loads(path.read_text())
    raw["delivery_count"] = 2
    assert parse_envelope(raw).is_redelivery


def test_an_envelope_with_an_unknown_field_still_parses():
    """A field the platform adds later must not stop a run."""
    import json
    from pathlib import Path

    from leash.models import parse_envelope

    path = Path(__file__).parent / "fixtures_live_envelope.json"
    raw = json.loads(path.read_text())
    raw["some_future_field"] = {"added": "later"}
    assert parse_envelope(raw).authorization_id
