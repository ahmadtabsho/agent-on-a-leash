"""A durable record of what the engine decided and why.

Two readers, one format. The control UI shows the customer what happened to
their money, and the demo needs to prove a decision after the fact. Both get
the same JSON Lines file: one object per decision, append-only, each carrying
the full evidence that produced it.

Append-only matters. A worker that restarts mid-run rebuilds its spend totals
and its already-answered set from this log, so a crash does not hand the agent
a fresh budget.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..decision.evidence import Verdict
from ..decision.state import RunState
from ..models.enums import Decision
from ..models.events import AuthorizationEvent


@dataclass
class DecisionRecord:
    run_id: str
    authorization_id: str
    source_authorization_id: str
    replay_order: int
    decided_at: str
    purchase_timestamp: str
    merchant_id: str
    merchant_name: str
    billing_amount_chf: float
    decision: str
    reason_codes: list[str]
    customer_message: str
    evidence: list[dict]
    elapsed_ms: float
    resolved_by_customer: str | None = None

    @classmethod
    def build(cls, run_id: str, event: AuthorizationEvent, verdict: Verdict) -> DecisionRecord:
        auth = event.authorization
        return cls(
            run_id=run_id,
            authorization_id=auth.authorization_id,
            source_authorization_id=auth.source_authorization_id,
            replay_order=auth.replay_order,
            decided_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            purchase_timestamp=auth.timestamp.isoformat().replace("+00:00", "Z"),
            merchant_id=auth.merchant.merchant_id,
            merchant_name=auth.merchant.merchant_name,
            billing_amount_chf=float(auth.billing_amount_chf),
            decision=verdict.decision.value,
            reason_codes=list(verdict.reason_codes),
            customer_message=verdict.customer_message,
            evidence=[f.to_payload() for f in verdict.findings],
            elapsed_ms=round(verdict.elapsed_ms, 3),
        )

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


class DecisionLog:
    """Append-only decision journal for one run."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.records: list[DecisionRecord] = []

    def append(self, record: DecisionRecord) -> DecisionRecord:
        self.records.append(record)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_json() + "\n")
        return record

    def note_resolution(self, authorization_id: str, decision: Decision) -> None:
        """Record the customer's own answer to a purchase we paused."""
        for record in self.records:
            if record.authorization_id == authorization_id:
                record.resolved_by_customer = decision.value
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "customer_resolution",
                            "authorization_id": authorization_id,
                            "decision": decision.value,
                            "at": datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z"),
                        }
                    )
                    + "\n"
                )

    @staticmethod
    def read(path: Path) -> Iterator[dict]:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)


def rebuild_state(path: Path, run_id: str = "recovered") -> RunState:
    """Reconstruct run memory from the journal after a restart.

    Without this, a worker that crashes mid-run comes back with an empty spend
    total and hands the agent its budget a second time.
    """
    state = RunState(run_id=run_id)
    resolutions: dict[str, Decision] = {}
    entries = list(DecisionLog.read(path))

    for entry in entries:
        if entry.get("type") == "customer_resolution":
            resolutions[entry["authorization_id"]] = Decision(entry["decision"])

    from decimal import Decimal

    from ..decision.state import Recorded

    for entry in entries:
        if entry.get("type") == "customer_resolution":
            continue
        authorization_id = entry["authorization_id"]
        decision = resolutions.get(authorization_id, Decision(entry["decision"]))
        state.records[authorization_id] = Recorded(
            authorization_id=authorization_id,
            decision=decision,
            billing_amount_chf=Decimal(str(entry["billing_amount_chf"])),
            timestamp=datetime.fromisoformat(entry["purchase_timestamp"].replace("Z", "+00:00")),
            merchant_id=entry["merchant_id"],
            fingerprint=entry.get("fingerprint", ""),
            awaiting_customer=decision is Decision.STEP_UP,
        )
    return state
