"""The parser must hold the contract's type traps, not approximate them.

Each test below corresponds to a way the brief says an implementation silently
goes wrong: a string amount coerced to a number, a four-digit MCC read as an
integer, a four-valued term read as a boolean, or a null read as zero.
"""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from leash.config import DATA_DIR
from leash.models import (
    AuthorizationStatus,
    Currency,
    Decision,
    EventContractError,
    OrderTerm,
    UncertaintyPolicy,
    parse_event,
)

FIXTURE = DATA_DIR / "scenario_fixtures" / "example_authorization_request.json"


@pytest.fixture
def raw() -> dict:
    with FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)


def mutate(raw: dict, path: str, value) -> dict:
    """Copy `raw` with one slash-separated path set to `value`."""
    clone = json.loads(json.dumps(raw))
    target = clone
    parts = path.split("/")
    for part in parts[:-1]:
        target = target[int(part)] if part.isdigit() else target[part]
    target[parts[-1]] = value
    return clone


def test_supplied_example_parses(raw):
    event = parse_event(raw)
    assert event.authorization.authorization_id == "AU_EXAMPLE_0001"
    assert event.authorization.currency is Currency.CHF
    assert event.mandate.uncertainty_policy is UncertaintyPolicy.ASK


# --- type traps ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("authorization/amount", "20.00"),           # amounts are numbers
        ("authorization/billing_amount_chf", "20.0"),
        ("authorization/items/0/unit_price", "18.00"),
        ("authorization/merchant/merchant_mcc", 5411),  # MCC is a string
        ("authorization/merchant/merchant_mcc", "541"),  # ...of four digits
        ("authorization/items/0/quantity", 1.5),     # counts are whole
        ("authorization/items/0/quantity", 0),       # ...and at least one
        ("authorization/replay_order", 0),
        ("authorization/recent_attempt_count_10m", -1),
        ("authorization/merchant/merchant_country", "ch"),  # ISO-3166 alpha-2
        ("authorization/order_returnable", True),    # a string, not a bool
        ("authorization/order_returnable", "yes"),
        ("authorization/merchant/recurring_capable", "unknown"),  # only true/false
        ("authorization/currency", "JPY"),
        ("authorization/initiator_type", "human"),   # live requests are agent
        ("authorization/items", []),                 # a cart has a line
        ("mandate/uncertainty_policy", "escalate"),
        ("type", "authorization.response"),
    ],
)
def test_contract_violations_are_rejected(raw, path, value):
    with pytest.raises(EventContractError):
        parse_event(mutate(raw, path, value))


def test_unknown_field_is_not_silently_dropped(raw):
    with pytest.raises(EventContractError):
        parse_event(mutate(raw, "authorization/spending_pre_approved", True))


def test_missing_required_field_is_rejected(raw):
    clone = json.loads(json.dumps(raw))
    del clone["authorization"]["delivery_by"]
    with pytest.raises(EventContractError):
        parse_event(clone)


def test_all_problems_are_reported_at_once(raw):
    clone = mutate(raw, "authorization/amount", "20.00")
    clone = mutate(clone, "authorization/merchant/merchant_mcc", 5411)
    with pytest.raises(EventContractError) as excinfo:
        parse_event(clone)
    assert len(excinfo.value.problems) == 2


# --- semantics the schema cannot express -----------------------------------


def test_null_period_spend_stays_null(raw):
    """`null` is "not supplied". Turning it into 0.0 would invent headroom."""
    event = parse_event(mutate(raw, "authorization/spend_in_period_before_chf", None))
    assert event.authorization.spend_in_period_before_chf is None


def test_unknown_return_term_is_not_permission(raw):
    event = parse_event(mutate(raw, "authorization/order_returnable", "unknown"))
    assert event.authorization.order_returnable is OrderTerm.UNKNOWN
    assert not event.authorization.order_returnable.is_stated


def test_not_applicable_is_distinct_from_false(raw):
    event = parse_event(mutate(raw, "authorization/order_cancellable", "not_applicable"))
    assert event.authorization.order_cancellable is OrderTerm.NOT_APPLICABLE
    assert not event.authorization.order_cancellable.is_stated


def test_amounts_are_decimal_not_float(raw):
    amount = parse_event(raw).authorization.billing_amount_chf
    assert isinstance(amount, Decimal)


def test_naive_timestamp_is_rejected(raw):
    """A spending window computed against a zoneless time is a wrong window."""
    with pytest.raises(EventContractError):
        parse_event(mutate(raw, "authorization/timestamp", "2026-08-12T08:59:59"))


def test_pending_purchase_is_not_yet_spend():
    assert not AuthorizationStatus.PENDING.is_final_spend
    assert AuthorizationStatus.APPROVED.is_final_spend
    assert not AuthorizationStatus.DECLINED.is_final_spend


def test_uncertainty_policy_maps_to_the_customers_own_choice():
    assert UncertaintyPolicy.ASK.to_decision() is Decision.STEP_UP
    assert UncertaintyPolicy.DECLINE.to_decision() is Decision.DECLINE
    assert UncertaintyPolicy.APPROVE.to_decision() is Decision.APPROVE


def test_deadline_is_measured_on_the_real_clock(raw):
    event = parse_event(raw)
    now = event.deadline_at - timedelta(seconds=3)
    assert event.seconds_until_deadline(now) == pytest.approx(3.0)
    assert event.deadline_at.tzinfo is not None


def test_line_totals_use_the_lines_own_currency(raw):
    clone = mutate(raw, "authorization/items/0/currency", "EUR")
    item = parse_event(clone).authorization.items[0]
    assert item.line_total == Decimal("18.00")
    assert item.line_total_chf == Decimal("17.10")  # 18.00 * 0.95


def test_connection_check_fixture_is_readable():
    """The pack's readable copy of AU0001 is not a complete live event."""
    with (DATA_DIR / "scenario_fixtures" / "connection_check.json").open() as fh:
        payload = json.load(fh)
    assert isinstance(payload, dict) and payload


def test_an_event_survives_a_json_round_trip(raw):
    """Serialising and reparsing must produce a contract-valid event.

    Pydantic writes Decimal as a JSON string by default, which would make our
    own output fail the schema it was built from.
    """
    once = parse_event(raw)
    twice = parse_event(json.loads(json.dumps(once.model_dump(mode="json"))))
    assert twice == once
    assert isinstance(once.model_dump(mode="json")["authorization"]["amount"], float)
