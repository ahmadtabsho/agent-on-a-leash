"""Closed vocabularies from the event schema and the decision contract.

Every one of these is an enum rather than a bare string because the schema
pins the allowed values, and because several of them are strings that *look*
like something else — `order_returnable` is the four-valued string
`"true"/"false"/"unknown"/"not_applicable"`, not a boolean, and reading it as
one silently turns "the seller did not say" into "yes".
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Decision(StrEnum):
    """What we send back to the platform."""

    APPROVE = "approve"
    DECLINE = "decline"
    STEP_UP = "step_up"


class UncertaintyPolicy(StrEnum):
    """What the customer told us to do when we cannot settle a purchase."""

    ASK = "ask"
    DECLINE = "decline"
    APPROVE = "approve"

    def to_decision(self) -> Decision:
        return {
            UncertaintyPolicy.ASK: Decision.STEP_UP,
            UncertaintyPolicy.DECLINE: Decision.DECLINE,
            UncertaintyPolicy.APPROVE: Decision.APPROVE,
        }[self]


class OrderTerm(StrEnum):
    """A purchase term the seller may or may not have stated.

    `UNKNOWN` means the seller did not supply it; `NOT_APPLICABLE` means the
    term does not apply to that kind of order. Neither is permission.
    """

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"

    @property
    def is_stated(self) -> bool:
        return self in (OrderTerm.TRUE, OrderTerm.FALSE)


class Attribution(StrEnum):
    """Who initiated a transaction.

    Live scenario purchases are always `AGENT`. The other two appear in the
    historical file: `HUMAN` is the customer, including cash withdrawals, and
    `MERCHANT` is a refund.
    """

    HUMAN = "human"
    AGENT = "agent"
    MERCHANT = "merchant"


class Currency(StrEnum):
    CHF = "CHF"
    EUR = "EUR"
    GBP = "GBP"
    USD = "USD"


class Channel(StrEnum):
    ECOMMERCE = "ecommerce"
    IN_STORE = "in_store"
    MOBILE_WALLET = "mobile_wallet"
    RECURRING = "recurring"
    ATM = "atm"


class Availability(StrEnum):
    ONLINE = "online"
    STORE = "store"
    STORE_AND_ONLINE = "store_and_online"
    ATM = "atm"


class AuthorityStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CardStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class MandateStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"


class AuthorizationStatus(StrEnum):
    """Outcome of an authorization we have already seen in this run."""

    APPROVED = "approved"
    DECLINED = "declined"
    PENDING = "pending"
    CANCELLED = "cancelled"

    @property
    def is_final_spend(self) -> bool:
        """Only a final approval counts against a spending limit.

        A purchase waiting for a human answer is not yet approved.
        """
        return self is AuthorizationStatus.APPROVED


class Operator(StrEnum):
    """Comparison operators a stored hard rule may use."""

    LT = "<"
    LTE = "<="
    EQ = "="
    NE = "!="
    GT = ">"
    GTE = ">="
    IN = "in"
    NOT_IN = "not_in"


class RuleScope(StrEnum):
    PURCHASE = "purchase"
    PERIOD = "period"
