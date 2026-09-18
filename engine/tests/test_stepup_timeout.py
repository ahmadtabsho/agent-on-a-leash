"""The window the customer has to answer.

The guide is explicit: "Do not invent a human answer." That single line decides
the whole design here. A decline we submit because nobody replied would be
recorded as the customer's decision when the customer never made one — so when
the window lapses, nothing is sent at all.

What lapsing *does* mean is that the purchase was never approved, its amount
was never spend, and the inbox should stop pretending the customer can still
answer it.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from leash.api import LeashClient
from leash.config import Settings
from leash.decision import DecisionEngine, RunState
from leash.models.enums import Decision
from leash.policy import compile_policy
from leash.replay import EventBuilder
from leash.worker import LapsedReviewError, Worker

SETTINGS = Settings(
    base_url="https://sandbox.invalid",
    api_key="test-key",
    decision_budget_ms=2500,
    llm_enabled=False,
    llm_model="",
    llm_timeout_ms=900,
)
HUMAN_WINDOW = 120.0


@pytest.fixture(scope="module")
def builder() -> EventBuilder:
    return EventBuilder()


class Sandbox:
    def __init__(self, builder, scenario_id):
        policy = compile_policy(builder.catalogue[scenario_id]["cardholder_instruction"])
        replay = builder.build(scenario_id, policy)
        self.queue = [
            {
                "run_id": "run_1",
                "event_id": i,
                "type": "authorization.request",
                "authorization_id": e.authorization.authorization_id,
                "status": "pending",
                "occurred_at": e.authorization.timestamp.isoformat().replace("+00:00", "Z"),
                "delivery_count": 1,
                "data": e.model_dump(mode="json"),
            }
            for i, e in enumerate(replay.events)
        ]
        self.decisions: list[dict] = []
        self.resolutions: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/bootstrap":
            return httpx.Response(
                200,
                json={
                    "timeouts": {
                        "decision_timeout_seconds": 8.0,
                        "human_timeout_seconds": HUMAN_WINDOW,
                    }
                },
            )
        if path == "/v1/decision-requests/next":
            return httpx.Response(200, json=self.queue.pop(0)) if self.queue else httpx.Response(204)
        if path.endswith("/decision"):
            import json

            self.decisions.append(json.loads(request.content))
            return httpx.Response(200, json={"recorded": True})
        if path.endswith("/resolve"):
            import json

            self.resolutions.append(json.loads(request.content))
            return httpx.Response(200, json={"recorded": True})
        return httpx.Response(404, json={"error": path})


def worker_for(sandbox) -> Worker:
    client = LeashClient(
        SETTINGS, httpx.Client(transport=httpx.MockTransport(sandbox.handler), base_url=SETTINGS.base_url)
    )
    return Worker(client, DecisionEngine(), RunState(), settings=SETTINGS)


def run(builder, scenario_id="SCEN0004"):
    sandbox = Sandbox(builder, scenario_id)
    worker = worker_for(sandbox)
    worker.run_until_idle(wait=0, max_empty_polls=1)
    return sandbox, worker


# --- the window comes from the service ------------------------------------


def test_the_window_is_read_from_bootstrap_not_assumed(builder):
    _, worker = run(builder)
    assert worker.human_timeout_seconds == HUMAN_WINDOW


def test_a_paused_purchase_carries_a_deadline(builder):
    _, worker = run(builder)
    review = next(iter(worker.pending.values()))
    assert review.expires_at is not None
    assert review.expires_at - review.raised_at == timedelta(seconds=HUMAN_WINDOW)


def test_seconds_remaining_is_reported_for_the_interface(builder):
    _, worker = run(builder)
    remaining = worker.time_remaining()
    assert remaining
    assert all(0 < left <= HUMAN_WINDOW for left in remaining.values())


def test_without_a_known_window_nothing_is_ever_treated_as_expired(builder):
    """Guessing the window would be worse than not tracking it."""
    sandbox = Sandbox(builder, "SCEN0004")

    def no_bootstrap(request):
        if request.url.path == "/v1/bootstrap":
            return httpx.Response(500, json={"error": "unavailable"})
        return sandbox.handler(request)

    client = LeashClient(SETTINGS, httpx.Client(transport=httpx.MockTransport(no_bootstrap)))
    worker = Worker(client, DecisionEngine(), RunState(), settings=SETTINGS)
    worker.run_until_idle(wait=0, max_empty_polls=1)

    assert worker.human_timeout_seconds is None
    review = next(iter(worker.pending.values()))
    assert review.expires_at is None
    assert not review.is_expired(datetime.now(timezone.utc) + timedelta(days=365))
    assert worker.sweep_expired(datetime.now(timezone.utc) + timedelta(days=365)) == []


# --- what happens when it lapses ------------------------------------------


def test_nothing_is_sent_to_the_platform_when_a_window_lapses(builder):
    """The load-bearing test. A decline sent because nobody replied would be
    recorded as the customer's decision, which they never made."""
    sandbox, worker = run(builder)
    decisions_before = len(sandbox.decisions)

    worker.sweep_expired(datetime.now(timezone.utc) + timedelta(seconds=HUMAN_WINDOW + 1))

    assert len(sandbox.decisions) == decisions_before, "no second automated decision"
    assert sandbox.resolutions == [], "and no answer invented on the customer's behalf"


def test_a_lapsed_review_leaves_the_inbox(builder):
    """Leaving it there would imply the customer can still answer."""
    _, worker = run(builder)
    waiting = set(worker.pending)
    assert waiting

    moved = worker.sweep_expired(datetime.now(timezone.utc) + timedelta(seconds=HUMAN_WINDOW + 1))

    assert {r.authorization_id for r in moved} == waiting
    assert worker.pending == {}
    assert set(worker.lapsed) == waiting
    assert all(r.lapsed for r in worker.lapsed.values())
    assert worker.stats.lapsed_reviews == len(waiting)


def test_a_lapsed_purchase_was_never_spend(builder):
    """It was paused, never approved. The limit must not have moved."""
    _, worker = run(builder)
    review = next(iter(worker.pending.values()))
    record = worker.state.answered(review.authorization_id)
    assert record.awaiting_customer

    worker.sweep_expired(datetime.now(timezone.utc) + timedelta(seconds=HUMAN_WINDOW + 1))

    still = worker.state.answered(review.authorization_id)
    assert still.decision is Decision.STEP_UP
    assert still.awaiting_customer, "a lapsed purchase is not an approval"


def test_a_purchase_inside_its_window_is_untouched(builder):
    _, worker = run(builder)
    waiting = set(worker.pending)

    assert worker.sweep_expired(datetime.now(timezone.utc) + timedelta(seconds=10)) == []
    assert set(worker.pending) == waiting


def test_an_answer_after_the_window_is_refused(builder):
    """The platform has stopped listening; pretending otherwise is a lie."""
    sandbox, worker = run(builder)
    target = next(iter(worker.pending))
    worker.sweep_expired(datetime.now(timezone.utc) + timedelta(seconds=HUMAN_WINDOW + 1))

    with pytest.raises(LapsedReviewError):
        worker.resolve(target, Decision.APPROVE)
    assert sandbox.resolutions == []


def test_an_answer_inside_the_window_still_works(builder):
    sandbox, worker = run(builder)
    target = next(iter(worker.pending))

    worker.resolve(target, Decision.APPROVE)

    assert len(sandbox.resolutions) == 1
    assert not worker.state.answered(target).awaiting_customer


def test_polling_sweeps_the_inbox_so_it_never_goes_stale(builder):
    """The sweep runs on every poll, not only when someone asks."""
    sandbox = Sandbox(builder, "SCEN0004")
    worker = worker_for(sandbox)
    worker.run_until_idle(wait=0, max_empty_polls=1)

    # Backdate the deadlines, then poll once more.
    for review in worker.pending.values():
        review.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    worker.run_until_idle(wait=0, max_empty_polls=1)

    assert worker.pending == {}
    assert worker.lapsed


def test_the_sweep_mirrors_the_platforms_own_transition(builder):
    """Measured against the live service: an unanswered purchase moves from
    `awaiting_customer` to `timed_out` by itself at exactly 120 seconds. Our
    sweep has to fire on the same boundary — earlier and the inbox drops a
    purchase the customer could still have answered, later and it offers a
    button the platform has stopped accepting."""
    _, worker = run(builder)
    # Each paused purchase carries its own clock, so the boundary is asserted
    # against one review rather than the batch.
    target = next(iter(worker.pending.values()))
    raised = target.raised_at

    just_before = worker.sweep_expired(raised + timedelta(seconds=HUMAN_WINDOW - 1))
    assert target.authorization_id not in {r.authorization_id for r in just_before}
    assert target.authorization_id in worker.pending, "answerable until the window closes"

    on_the_boundary = worker.sweep_expired(raised + timedelta(seconds=HUMAN_WINDOW))
    assert target.authorization_id in {r.authorization_id for r in on_the_boundary}
    assert target.authorization_id in worker.lapsed
    assert target.authorization_id not in worker.pending
