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
from datetime import datetime, timezone
from pathlib import Path

from ..api.client import ApiError, LeashClient
from ..config import Settings
from ..decision import DecisionEngine, RunState
from ..models import EventContractError, parse_envelope
from ..models.enums import Decision
from ..replay.log import DecisionLog, DecisionRecord


@dataclass
class PendingReview:
    """A purchase paused for the customer."""

    authorization_id: str
    source_authorization_id: str
    merchant_name: str
    billing_amount_chf: float
    customer_message: str
    evidence: list[dict]
    raised_at: datetime


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
            self.pending[auth.authorization_id] = PendingReview(
                authorization_id=auth.authorization_id,
                source_authorization_id=auth.source_authorization_id,
                merchant_name=auth.merchant.merchant_name,
                billing_amount_chf=float(auth.billing_amount_chf),
                customer_message=verdict.customer_message,
                evidence=[f.to_payload() for f in verdict.findings],
                raised_at=datetime.now(timezone.utc),
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

    # --- the customer's own answer ----------------------------------------

    def resolve(self, authorization_id: str, decision: Decision, message: str = "") -> dict:
        """Send the customer's approve or decline for a paused purchase."""
        if decision not in (Decision.APPROVE, Decision.DECLINE):
            raise ValueError("a customer answers approve or decline, nothing else")
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

        while consecutive_empty < max_empty_polls:
            if deadline_seconds and time.monotonic() - started > deadline_seconds:
                break
            try:
                polled = self.client.next_request(wait=wait)
            except ApiError:
                self.stats.api_errors += 1
                raise

            self.stats.polled += 1
            if not polled.has_work:
                self.stats.empty_polls += 1
                consecutive_empty += 1
                continue

            consecutive_empty = 0
            self.handle(polled.envelope)

        return self.stats
