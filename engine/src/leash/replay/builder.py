"""Rebuild live-shaped events from the CSV pack.

The pack's own `connection_check.json` is a *readable* copy of one attempt — it
has string amounts and a flat shape, and it does not validate against the event
schema. So offline events are assembled here from the CSVs instead, following
the conversion rules the guide sets out: amounts and counts become numbers,
strings stay strings, empty nullable fields become `null` rather than `0` or
`""`, and the nested shop and cart objects are built from the joins.

Two clocks are kept apart, because conflating them is a documented trap.
`timestamp` is simulated purchase time and is preserved exactly — spending
windows and velocity are computed from it. `deadline_at` and `received_at` are
real-clock values and are issued fresh at build time.

Live IDs are also kept apart. Each run mints its own `authorization_id`, while
`source_authorization_id` keeps the `AU...` row key, and a non-null
`related_authorization_id` is rewritten to the related purchase's live ID in
this run — the same mapping the API performs.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..datapack import load_table
from ..models.events import AuthorizationEvent
from ..models.validation import parse_event
from ..policy.compiler import CompiledPolicy

DEFAULT_DEADLINE_SECONDS = 8


def _number(raw: str) -> float:
    return float(raw)


def _optional_number(raw: str) -> float | None:
    return float(raw) if raw not in ("", None) else None


def _optional(raw: str) -> str | None:
    return raw if raw not in ("", None) else None


@dataclass
class ScenarioReplay:
    """One scenario's attempts, in delivery order, with its instruction."""

    scenario_id: str
    scenario_name: str
    cardholder_instruction: str
    customer_id: str
    card_id: str
    events: list[AuthorizationEvent]


class EventBuilder:
    """Assembles live-shaped events for a scenario."""

    def __init__(self) -> None:
        self.attempts = load_table("purchase_attempts")
        self.merchants = {m["merchant_id"]: m for m in load_table("merchants")}
        self.authorities = {a["authority_id"]: a for a in load_table("scenario_authorities")}
        self.catalogue = {s["scenario_id"]: s for s in load_table("scenario_catalogue")}
        self.lines: dict[str, list[dict]] = defaultdict(list)
        for row in load_table("purchase_attempt_items"):
            self.lines[row["authorization_id"]].append(row)

    def scenario_ids(self) -> list[str]:
        return sorted(self.catalogue)

    def build(
        self,
        scenario_id: str,
        policy: CompiledPolicy,
        *,
        mandate_id: str | None = None,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    ) -> ScenarioReplay:
        if scenario_id not in self.catalogue:
            raise KeyError(f"unknown scenario {scenario_id!r}")
        catalogue = self.catalogue[scenario_id]

        rows = sorted(
            (r for r in self.attempts if r["scenario_id"] == scenario_id),
            key=lambda r: int(r["replay_order"]),
        )
        if not rows:
            raise ValueError(f"{scenario_id} has no attempts")

        authority = self.authorities[rows[0]["authority_id"]]
        run_tag = uuid.uuid4().hex[:8]
        mandate_id = mandate_id or f"TM_LOCAL_{run_tag}"
        profile_id = f"PROFILE_LOCAL_{run_tag}"

        # Live IDs are minted per run; the AU key stays as the source ID.
        live_ids = {r["authorization_id"]: f"AUL_{run_tag}_{int(r['replay_order']):03d}" for r in rows}

        mandate = {
            "mandate_id": mandate_id,
            "status": "active",
            "customer_id": authority["customer_id"],
            "card_id": authority["card_id"],
            "instruction": policy.instruction,
            "hard_rules": [rule.to_payload() for rule in policy.hard_rules],
            "uncertainty_policy": policy.uncertainty_policy.value,
            "profile_id": profile_id,
        }

        events: list[AuthorizationEvent] = []
        for row in rows:
            received = datetime.now(timezone.utc)
            payload = {
                "type": "authorization.request",
                "request_id": f"req_{run_tag}_{int(row['replay_order']):03d}",
                "deadline_at": _iso(received + timedelta(seconds=deadline_seconds)),
                "authorization": self._authorization(row, live_ids, mandate_id, profile_id),
                "mandate": mandate,
                "context": {
                    # Built by the caller's own ledger during replay, not by the
                    # fixture: the fixture has no idea what we decided.
                    "approved_spend_in_period_chf": None,
                    "recent_authorizations": [],
                },
                "runtime": {
                    "received_at": _iso(received),
                    "history_window_minutes": 10,
                    "context_basis": "run_decisions_and_scenario_timestamps",
                },
            }
            events.append(parse_event(payload))

        return ScenarioReplay(
            scenario_id=scenario_id,
            scenario_name=catalogue["scenario_name"],
            cardholder_instruction=catalogue["cardholder_instruction"],
            customer_id=authority["customer_id"],
            card_id=authority["card_id"],
            events=events,
        )

    def _authorization(
        self, row: dict, live_ids: dict[str, str], mandate_id: str, profile_id: str
    ) -> dict:
        merchant = self.merchants[row["merchant_id"]]
        related = _optional(row["related_authorization_id"])
        return {
            "authorization_id": live_ids[row["authorization_id"]],
            "source_authorization_id": row["authorization_id"],
            "scenario_id": row["scenario_id"],
            "replay_order": int(row["replay_order"]),
            "mandate_id": mandate_id,
            "profile_id": profile_id,
            "card_id": row["card_id"],
            "initiator_type": "agent",
            "merchant": {
                "merchant_id": merchant["merchant_id"],
                "merchant_name": merchant["merchant_name"],
                "merchant_category": merchant["merchant_category"],
                "merchant_mcc": merchant["merchant_mcc"],
                "merchant_country": merchant["merchant_country"],
                "merchant_city": merchant["merchant_city"],
                "availability": merchant["availability"],
                "recurring_capable": merchant["recurring_capable"],
            },
            "timestamp": row["timestamp"],
            "amount": _number(row["amount"]),
            "currency": row["currency"],
            "billing_amount_chf": _number(row["billing_amount_chf"]),
            "items_subtotal": _number(row["items_subtotal"]),
            "delivery_fee": _number(row["delivery_fee"]),
            "channel": row["channel"],
            "customer_device_id": row["customer_device_id"],
            "authority_status": row["authority_status"],
            "card_status_at_attempt": row["card_status_at_attempt"],
            "spend_in_period_before_chf": _optional_number(row["spend_in_period_before_chf"]),
            "recent_attempt_count_10m": int(row["recent_attempt_count_10m"]),
            "fulfillment_method": row["fulfillment_method"],
            "delivery_by": _optional(row["delivery_by"]),
            "order_returnable": row["order_returnable"],
            "order_cancellable": row["order_cancellable"],
            # Rewritten to this run's live ID, as the API does.
            "related_authorization_id": live_ids.get(related) if related else None,
            "related_authorization_status": _optional(row["related_authorization_status"]),
            "purchase_description": row["purchase_description"],
            "items": [
                {
                    "line_no": int(line["line_no"]),
                    "item_id": line["item_id"],
                    "item_name": line["item_name"],
                    "item_category": line["item_category"],
                    "quantity": int(line["quantity"]),
                    "unit_price": _number(line["unit_price"]),
                    "currency": line["currency"],
                    "item_details": line["item_details"],
                }
                for line in sorted(
                    self.lines[row["authorization_id"]], key=lambda line: int(line["line_no"])
                )
            ],
        }


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
