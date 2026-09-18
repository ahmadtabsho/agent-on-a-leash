"""What the engine remembers inside one run.

Three things have to survive between purchases, and each maps to a specific
way the challenge says an implementation goes wrong:

* **Approved spend.** A rolling limit needs the running total, and only final
  approvals count toward it. A purchase waiting for a human answer is not yet
  spend, so it is held separately and only moves once the customer answers.
* **Answers already given.** The same purchase can be delivered twice. It is
  recognised by its live `authorization_id`, and the saved answer is replayed
  rather than recomputed — otherwise a retry charges the limit twice.
* **What each basket looked like.** "Different IDs can still describe an
  unwanted duplicate order", so a fingerprint of shop, cart and amount is kept
  to catch a repeat that carries a new ID.

Spending windows run on simulated purchase time, never the real clock.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta
from decimal import Decimal

from ..models.enums import Decision
from ..models.events import Authorization


@dataclass(frozen=True)
class Recorded:
    """One purchase this run has already answered."""

    authorization_id: str
    decision: Decision
    billing_amount_chf: Decimal
    timestamp: datetime
    merchant_id: str
    fingerprint: str
    awaiting_customer: bool = False


def fingerprint(authorization: Authorization) -> str:
    """Identify a basket by what it is, not by which ID it arrived under."""
    lines = sorted(
        f"{item.item_id}:{item.quantity}:{item.unit_price}" for item in authorization.items
    )
    material = "|".join(
        [
            authorization.merchant.merchant_id,
            str(authorization.billing_amount_chf),
            *lines,
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


@dataclass
class RunState:
    """Decisions made so far in one run."""

    run_id: str = "offline"
    records: dict[str, Recorded] = dc_field(default_factory=dict)

    # --- idempotency -------------------------------------------------------

    def answered(self, authorization_id: str) -> Recorded | None:
        """The answer already given for this live purchase, if any."""
        return self.records.get(authorization_id)

    def record(
        self,
        authorization: Authorization,
        decision: Decision,
    ) -> Recorded:
        """Save an answer. Re-recording the same purchase overwrites, never adds."""
        recorded = Recorded(
            authorization_id=authorization.authorization_id,
            decision=decision,
            billing_amount_chf=Decimal(authorization.billing_amount_chf),
            timestamp=authorization.timestamp,
            merchant_id=authorization.merchant.merchant_id,
            fingerprint=fingerprint(authorization),
            awaiting_customer=decision is Decision.STEP_UP,
        )
        self.records[recorded.authorization_id] = recorded
        return recorded

    def resolve(self, authorization_id: str, decision: Decision) -> Recorded | None:
        """Apply the customer's answer to a purchase we paused.

        Only now can the amount count toward a spending limit.
        """
        existing = self.records.get(authorization_id)
        if existing is None:
            return None
        updated = Recorded(
            authorization_id=existing.authorization_id,
            decision=decision,
            billing_amount_chf=existing.billing_amount_chf,
            timestamp=existing.timestamp,
            merchant_id=existing.merchant_id,
            fingerprint=existing.fingerprint,
            awaiting_customer=False,
        )
        self.records[authorization_id] = updated
        return updated

    # --- spend -------------------------------------------------------------

    def approved_spend_within(self, days: int, as_of: datetime) -> Decimal:
        """Finally approved spend in the window ending at `as_of`.

        `as_of` is simulated purchase time. A purchase still waiting for the
        customer contributes nothing.
        """
        since = as_of - timedelta(days=days)
        total = Decimal(0)
        for record in self.records.values():
            if record.awaiting_customer or record.decision is not Decision.APPROVE:
                continue
            if since < record.timestamp <= as_of:
                total += record.billing_amount_chf
        return total

    def pending_spend_within(self, days: int, as_of: datetime) -> Decimal:
        """Amounts paused for the customer inside the same window.

        Not spend, but worth showing: it is what the limit would become.
        """
        since = as_of - timedelta(days=days)
        return sum(
            (
                r.billing_amount_chf
                for r in self.records.values()
                if r.awaiting_customer and since < r.timestamp <= as_of
            ),
            Decimal(0),
        )

    # --- duplicates --------------------------------------------------------

    def matching_baskets(
        self, authorization: Authorization, *, within: timedelta = timedelta(days=1)
    ) -> list[Recorded]:
        """Earlier purchases in this run describing the same basket."""
        target = fingerprint(authorization)
        since = authorization.timestamp - within
        return [
            record
            for record in self.records.values()
            if record.fingerprint == target
            and record.authorization_id != authorization.authorization_id
            and since <= record.timestamp <= authorization.timestamp
        ]
