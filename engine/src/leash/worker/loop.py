"""The worker: keep receiving purchases and answering them before the deadline.

The guide is blunt about the shape of this. The decision deadline is eight
seconds *from when the request was queued*, including time before we polled, so
the deadline is already partly spent when the event arrives. Reading a request
by hand and typing an answer is too slow; this loop exists so nothing waits on
a person.

Three things it must get right, each with a specific wrong answer:

* **A `204` is not the end of the run.** It means there was no work in that
  polling window. The loop checks progress and polls again.
* **A purchase delivered twice gets the same answer.** Recognised by its live
  `authorization_id`; the saved answer is re-sent rather than recomputed, so a
  retry does not charge a spending limit twice.
* **A `step_up` is not an answer.** It pauses the purchase and hands it to the
  customer. The loop keeps polling while the interface waits, and the
  customer's reply goes back through `/resolve` — never as a second automated
  decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..api.client import ApiError, LeashClient
from ..config import Settings
from ..decision import DecisionEngine, RunState
from ..models import EventContractError, parse_envelope
from ..models.enums import Decision
from ..replay.log import DecisionLog, DecisionRecord


class LapsedReviewError(RuntimeError):
    """An answer arrived after the customer's window had already closed."""


@dataclass
class PendingReview:
    """A purchase paused for the customer, and how long they have.

    The platform gives the customer a fixed window — 120 seconds by default,
    read from `/v1/bootstrap` rather than assumed. When it lapses the purchase
    is simply not approved; see `Worker.sweep_expired` for why nothing is sent
    on the customer's behalf.
    """

    authorization_id: str
    source_authorization_id: str
    merchant_name: str
    billing_amount_chf: float
    customer_message: str
    evidence: list[dict]
    raised_at: datetime
    expires_at: datetime | None = None
    lapsed: bool = False

    def seconds_left(self, now: datetime) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - now).total_seconds()

    def is_expired(self, now: datetime) -> bool:
        left = self.seconds_left(now)
        return left is not None and left <= 0


@dataclass
class WorkerStats:
    polled: int = 0
    empty_polls: int = 0
    decided: int = 0
    replayed: int = 0
    parse_failures: int = 0
    api_errors: int = 0
    by_decision: dict[str, int] = dc_field(
        default_factory=lambda: {d.value: 0 for d in Decision}
    )
    slowest_ms: float = 0.0
    missed_deadlines: int = 0
    lapsed_reviews: int = 0


class Worker:
    """Polls the sandbox and answers every purchase it is handed."""

    def __init__(
        self,
        client: LeashClient,
        engine: DecisionEngine | None = None,
        state: RunState | None = None,
        *,
        log_path: Path | None = None,
        settings: Settings | None = None,
    ):
        self.client = client
        self.engine = engine or DecisionEngine()
        self.state = state or RunState()
        self.settings = settings or Settings.from_env()
        self.log = DecisionLog(log_path)
        self.stats = WorkerStats()
        self.pending: dict[str, PendingReview] = {}
        self.lapsed: dict[str, PendingReview] = {}
        # Filled from /v1/bootstrap by `adopt_timeouts`. None until then, and a
        # review with no deadline is never treated as expired — guessing the
        # window would be worse than not tracking it.
        self.human_timeout_seconds: float | None = None

    def adopt_timeouts(self) -> float | None:
        """Read the human window from the service rather than assuming it.

        `LEASH_HUMAN_TIMEOUT_SECONDS` shortens it for demonstration, so a
        lapse can be shown in seconds rather than two minutes of dead air. It
        only ever shortens: the platform still times the purchase out on its
        own schedule, so a longer local window would promise the customer time
        they do not have.
        """
        override = self.settings.human_timeout_override_s
        try:
            timeouts = self.client.bootstrap().get("timeouts", {})
        except ApiError:
            # Reporting, not correctness. A run works without this.
            return self.settings.human_timeout_override_s
        value = timeouts.get("human_timeout_seconds")
        if isinstance(value, (int, float)) and value > 0:
            self.human_timeout_seconds = float(value)
        if override:
            reported = self.human_timeout_seconds
            self.human_timeout_seconds = min(override, reported) if reported else override
        return self.human_timeout_seconds

    # --- one purchase ------------------------------------------------------

    def handle(self, raw_envelope: dict) -> Decision | None:
        """Decide one delivered purchase and record the platform's answer."""
        try:
            envelope = parse_envelope(raw_envelope)
        except EventContractError as exc:
            # An event we cannot fully read is never decided on. It is escalated
            # so a person sees it, and the reason is kept verbatim.
            self.stats.parse_failures += 1
            authorization_id = str(raw_envelope.get("authorization_id", "")) or None
            if authorization_id:
                self._escalate_unreadable(authorization_id, exc)
            return None

        event = envelope.data
        auth = event.authorization
        self.state.run_id = envelope.run_id

        verdict = self.engine.decide(event, self.state)
        if verdict.reason_codes == ["idempotent_replay"]:
            self.stats.replayed += 1

        remaining = event.seconds_until_deadline(datetime.now(timezone.utc))
        if remaining <= 0:
            self.stats.missed_deadlines += 1

        self.stats.decided += 1
        self.stats.by_decision[verdict.decision.value] += 1
        self.stats.slowest_ms = max(self.stats.slowest_ms, verdict.elapsed_ms)
        self.log.append(DecisionRecord.build(envelope.run_id, event, verdict))

        try:
            self.client.submit_decision(
                auth.authorization_id, verdict.to_decision_payload(auth.authorization_id)
            )
        except ApiError:
            self.stats.api_errors += 1
            raise

        if verdict.decision is Decision.STEP_UP:
            raised = datetime.now(timezone.utc)
            window = self.human_timeout_seconds
            self.pending[auth.authorization_id] = PendingReview(
                authorization_id=auth.authorization_id,
                source_authorization_id=auth.source_authorization_id,
                merchant_name=auth.merchant.merchant_name,
                billing_amount_chf=float(auth.billing_amount_chf),
                customer_message=verdict.customer_message,
                evidence=[f.to_payload() for f in verdict.findings],
                raised_at=raised,
                expires_at=raised + timedelta(seconds=window) if window else None,
            )
        return verdict.decision

    def _escalate_unreadable(self, authorization_id: str, exc: EventContractError) -> None:
        payload = {
            "authorization_id": authorization_id,
            "decision": Decision.STEP_UP.value,
            "reason_codes": ["customer_confirmation", "event_not_understood"],
            "customer_message": (
                "We could not read this purchase request in full, so we have not "
                "acted on it. Please review it yourself."
            ),
            "evidence": [{"code": "event_not_understood", "detail": p} for p in exc.problems[:5]],
            "engine_version": self.engine.config.engine_version,
        }
        try:
            self.client.submit_decision(authorization_id, payload)
        except ApiError:
            self.stats.api_errors += 1

    # --- the window the customer has ---------------------------------------

    def sweep_expired(self, now: datetime | None = None) -> list[PendingReview]:
        """Move reviews whose window has lapsed out of the inbox.

        Nothing is sent to the platform. That is the whole point: the guide is
        explicit that we must not "invent a human answer", and a decline we
        submit because nobody replied is exactly that — it would be recorded as
        the customer's decision when the customer never made one.

        Measured against the live service rather than assumed: a purchase left
        unanswered moves from `awaiting_customer` to `timed_out` on its own at
        exactly the 120-second mark. The platform already handles the lapse
        correctly, so anything we sent would overwrite that with a decision
        nobody made. This sweep only mirrors the transition locally.

        So a lapsed review is not an answer, it is the *absence* of one. The
        purchase was never approved, its amount was never spend, and the
        customer is told plainly that we asked and the window closed. Leaving
        it in the inbox would be worse: it would look like it is still theirs
        to answer when the platform has stopped listening.
        """
        now = now or datetime.now(timezone.utc)
        moved: list[PendingReview] = []
        for authorization_id, review in list(self.pending.items()):
            if not review.is_expired(now):
                continue
            review.lapsed = True
            self.lapsed[authorization_id] = self.pending.pop(authorization_id)
            self.stats.lapsed_reviews += 1
            moved.append(review)
        return moved

    def time_remaining(self, now: datetime | None = None) -> dict[str, float | None]:
        """Seconds left on each waiting purchase, for the interface to show."""
        now = now or datetime.now(timezone.utc)
        return {a: r.seconds_left(now) for a, r in self.pending.items()}

    # --- the customer's own answer ----------------------------------------

    def resolve(self, authorization_id: str, decision: Decision, message: str = "") -> dict:
        """Send the customer's approve or decline for a paused purchase."""
        if decision not in (Decision.APPROVE, Decision.DECLINE):
            raise ValueError("a customer answers approve or decline, nothing else")
        if authorization_id in self.lapsed:
            raise LapsedReviewError(
                f"{authorization_id} was asked about but the customer's window closed; "
                "the platform is no longer accepting an answer for it"
            )
        payload = {
            "decision": decision.value,
            "customer_message": message or f"The customer chose to {decision.value} this purchase.",
            "evidence": [],
        }
        response = self.client.resolve(authorization_id, payload)
        self.state.resolve(authorization_id, decision)
        self.log.note_resolution(authorization_id, decision)
        self.pending.pop(authorization_id, None)
        return response

    # --- the loop ----------------------------------------------------------

    def run_until_idle(
        self,
        *,
        wait: int = 25,
        max_empty_polls: int = 3,
        deadline_seconds: float | None = None,
    ) -> WorkerStats:
        """Keep answering while work remains.

        Stops after `max_empty_polls` consecutive empty windows, which is the
        closest a client can get to "the run has nothing more for us" — a single
        `204` never means that.
        """
        started = time.monotonic()
        consecutive_empty = 0
        if self.human_timeout_seconds is None:
            self.adopt_timeouts()

        while consecutive_empty < max_empty_polls:
            if deadline_seconds and time.monotonic() - started > deadline_seconds:
                break
            try:
                polled = self.client.next_request(wait=wait)
            except ApiError:
                self.stats.api_errors += 1
                raise

            self.stats.polled += 1
            self.sweep_expired()
            if not polled.has_work:
                self.stats.empty_polls += 1
                consecutive_empty += 1
                continue

            consecutive_empty = 0
            self.handle(polled.envelope)

        return self.stats
