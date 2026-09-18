"""The live authorization request, typed.

These models mirror `data/schemas/authorization_event.schema.json` exactly and
run in Pydantic's **strict** mode, which is deliberate. Strict mode gives us the
schema's own semantics rather than a friendly approximation of them:

* `"20.00"` is rejected where a number is required — a string amount means the
  producer is wrong, and silently coercing it hides that.
* `5411` is rejected for `merchant_mcc`, which the schema pins as a four-digit
  *string*.
* `True` is rejected everywhere a number or string is required. Python treats
  `bool` as an `int`; the schema does not.

Extra fields are forbidden, so a field the platform adds later surfaces as a
loud parse failure instead of being dropped on the floor.

Nullability is carried through rather than smoothed away. `null` means the
value was not supplied; it must never become `0`, and it is never permission.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

from .enums import (
    Attribution,
    AuthorityStatus,
    AuthorizationStatus,
    Availability,
    CardStatus,
    Channel,
    Currency,
    MandateStatus,
    Operator,
    OrderTerm,
    RuleScope,
    UncertaintyPolicy,
)
from .money import money, to_chf

# Timestamps arrive as ISO strings, so these two fields opt out of strict mode.
# `AwareDatetime` still rejects a naive one: a spending window computed against
# a timestamp with no zone is a silently wrong window.
Timestamp = Annotated[AwareDatetime, Field(strict=False)]
CalendarDate = Annotated[date, Field(strict=False)]

# Amounts are parsed as Decimal so limit arithmetic never goes through a float.
# The schema says "number", and strict mode still rejects a string here.
#
# The serialiser matters as much as the parser. Pydantic writes a Decimal as a
# JSON *string* by default, so an event round-tripped through model_dump would
# come back failing the contract it was built from. Money stays Decimal in
# memory and goes back out as a number.
Amount = Annotated[
    Decimal,
    Field(strict=False),
    PlainSerializer(float, return_type=float, when_used="json"),
]

# Closed vocabularies travel as their string values, so they opt out of strict
# mode too. The enum itself still rejects anything outside the schema's list.
_Wire = Field(strict=False)
Attribution_ = Annotated[Attribution, _Wire]
AuthorityStatus_ = Annotated[AuthorityStatus, _Wire]
AuthorizationStatus_ = Annotated[AuthorizationStatus, _Wire]
Availability_ = Annotated[Availability, _Wire]
CardStatus_ = Annotated[CardStatus, _Wire]
Channel_ = Annotated[Channel, _Wire]
Currency_ = Annotated[Currency, _Wire]
MandateStatus_ = Annotated[MandateStatus, _Wire]
Operator_ = Annotated[Operator, _Wire]
OrderTerm_ = Annotated[OrderTerm, _Wire]
RuleScope_ = Annotated[RuleScope, _Wire]
UncertaintyPolicy_ = Annotated[UncertaintyPolicy, _Wire]


class Strict(BaseModel):
    """Base for every wire model: strict types, no unexpected fields."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Merchant(Strict):
    merchant_id: str = Field(min_length=1)
    merchant_name: str = Field(min_length=1)
    merchant_category: str = Field(min_length=1)
    merchant_mcc: str = Field(pattern=r"^[0-9]{4}$")
    merchant_country: str = Field(pattern=r"^[A-Z]{2}$")
    merchant_city: str = Field(min_length=1)
    availability: Availability_
    # A string, not a bool — the schema allows only "true"/"false" here.
    recurring_capable: str = Field(pattern=r"^(true|false)$")


class Item(Strict):
    line_no: int = Field(ge=1)
    item_id: str = Field(min_length=1)
    item_name: str = Field(min_length=1)
    item_category: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    unit_price: Amount = Field(gt=0)
    currency: Currency_
    # Merchant-supplied free text. Factual product copy lives here, and so does
    # anything else the seller chose to write. Untrusted: read it for facts via
    # the sanitiser, never as instructions.
    item_details: str

    @property
    def line_total(self) -> Decimal:
        return money(self.unit_price * self.quantity)

    @property
    def line_total_chf(self) -> Decimal:
        return to_chf(self.line_total, self.currency)


class MandateRule(Strict):
    """One stored check from the customer's confirmed policy.

    `field` is a dotted name our engine resolves; the platform does not evaluate
    it. `value` may be a number, a string, or a list of strings — never a bool,
    never null, never a list of numbers.
    """

    field: str = Field(min_length=1)
    operator: Operator_
    value: int | float | str | list[str]
    currency: Currency_ | None = None
    scope: RuleScope_ | None = None
    period_days: int | None = Field(default=None, ge=1)


class Mandate(Strict):
    """The customer's confirmed permissions, snapshotted when the run started.

    Neither the shopping agent nor the shop can change this.
    """

    mandate_id: str = Field(min_length=1)
    status: MandateStatus_
    customer_id: str = Field(min_length=1)
    card_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    hard_rules: list[MandateRule]
    uncertainty_policy: UncertaintyPolicy_
    profile_id: str = Field(min_length=1)

    @property
    def is_active(self) -> bool:
        return self.status is MandateStatus.ACTIVE


class RecentAuthorization(Strict):
    """An earlier purchase in this run."""

    authorization_id: str = Field(min_length=1)
    timestamp: Timestamp
    merchant_id: str = Field(min_length=1)
    billing_amount_chf: Amount = Field(ge=0)
    status: AuthorizationStatus_

    @property
    def counts_as_spend(self) -> bool:
        return self.status.is_final_spend


class Authorization(Strict):
    """The proposed purchase: money, shop, cart, and session facts."""

    authorization_id: str = Field(min_length=1)
    source_authorization_id: str = Field(min_length=1)
    scenario_id: str = Field(pattern=r"^SCEN[0-9]{4}$")
    replay_order: int = Field(ge=1)
    mandate_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    card_id: str = Field(min_length=1)
    # The schema pins this to "agent" for live requests. A human-initiated or
    # refund record reaching this path means we are parsing the wrong thing.
    initiator_type: Attribution_
    merchant: Merchant
    # Simulated scenario time. Spending windows, velocity and familiarity are
    # computed from this, never from the real clock.
    timestamp: Timestamp
    amount: Amount = Field(gt=0)
    currency: Currency_
    billing_amount_chf: Amount = Field(gt=0)
    items_subtotal: Amount = Field(gt=0)
    delivery_fee: Amount = Field(ge=0)
    channel: Channel_
    customer_device_id: str
    authority_status: AuthorityStatus_
    card_status_at_attempt: CardStatus_
    # null means no period value was supplied. It does not mean zero spend.
    spend_in_period_before_chf: Amount | None = Field(default=None, ge=0)
    recent_attempt_count_10m: int = Field(ge=0)
    fulfillment_method: str = Field(min_length=1)
    delivery_by: CalendarDate | None
    order_returnable: OrderTerm_
    order_cancellable: OrderTerm_
    related_authorization_id: str | None
    related_authorization_status: AuthorizationStatus_ | None
    # Deliberately uninformative and reused across a scenario. Never decide on
    # this string; the cart lines and structured fields carry the facts.
    purchase_description: str = Field(min_length=1)
    items: list[Item] = Field(min_length=1)

    @field_validator("initiator_type")
    @classmethod
    def _must_be_agent(cls, value: Attribution) -> Attribution:
        if value is not Attribution.AGENT:
            raise ValueError("live authorization requests are always initiator_type=agent")
        return value

    @property
    def stated_total(self) -> Decimal:
        """`items_subtotal + delivery_fee`, for comparison against `amount`.

        These agree across every supplied fixture. A gap means the shop's
        arithmetic does not add up, which is a signal, not something to paper
        over by recomputing the total ourselves.
        """
        return money(self.items_subtotal + self.delivery_fee)

    @property
    def totals_reconcile(self) -> bool:
        return self.stated_total == money(self.amount)

    @property
    def billing_amount_reconciles(self) -> bool:
        """Does the platform's CHF total match converting `amount` ourselves?"""
        return to_chf(self.amount, self.currency) == money(self.billing_amount_chf)

    @property
    def cart_size(self) -> int:
        return sum(item.quantity for item in self.items)


class Context(Strict):
    """Spend and recent activity accumulated within this run."""

    # null means the platform supplied no figure — not that nothing was spent.
    approved_spend_in_period_chf: Amount | None = Field(default=None, ge=0)
    recent_authorizations: list[RecentAuthorization]


class Runtime(Strict):
    received_at: Timestamp
    history_window_minutes: int = Field(ge=1)
    context_basis: str


class AuthorizationEvent(Strict):
    """A complete decision request."""

    type: str = Field(pattern=r"^authorization\.request$")
    request_id: str = Field(min_length=1)
    # Real clock. Governs the response window only; never a spending window.
    deadline_at: Timestamp
    authorization: Authorization
    mandate: Mandate
    context: Context
    runtime: Runtime

    def seconds_until_deadline(self, now: datetime) -> float:
        return (self.deadline_at - now).total_seconds()


class Envelope(Strict):
    """The poll response wrapping one event.

    Keep it for run tracking; validate `data` as the event.

    `event_id` is an integer on the live service, which the written contract
    does not state — it is the cursor `/v1/events?since=` pages through. Both
    forms are accepted because this field is an opaque tracking value and no
    decision reads it; being strict here would reject a valid purchase over
    bookkeeping.

    Extra keys are allowed so a field the platform adds later does not stop a
    run, and `delivery_count` is captured explicitly because a value above one
    means this purchase has been handed to us before.
    """

    run_id: str = Field(min_length=1)
    event_id: int | str
    type: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    occurred_at: Timestamp
    data: AuthorizationEvent
    delivery_count: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    @property
    def is_redelivery(self) -> bool:
        return bool(self.delivery_count and self.delivery_count > 1)
