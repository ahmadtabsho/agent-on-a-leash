"""Resolve the facts a rule asks about.

A rule names a field; this is where that name becomes a value. Three
principles run through all of it:

* **Not established is not false.** A seller who states no return window has
  not offered a zero-day one. Those cases resolve to `None` and become
  uncertainty, which the customer's own policy then settles.
* **Identifiers over names.** Familiarity is matched on `merchant_id`. A name
  is the one thing an impostor controls.
* **Merchant text is a source, not an authority.** Facts are read from it, and
  when that text also tries to steer the decision, everything read from it
  stops counting as established.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models.enums import Operator, OrderTerm
from ..models.events import AuthorizationEvent, Item
from ..models.money import to_chf
from .history import CardHistory, Lookalike
from .sanitize import Sanitised, sanitise
from .state import RunState

STOPWORDS = frozenset(
    {"a", "an", "the", "of", "for", "with", "and", "or", "in", "my", "our", "one", "new"}
)

# How many prior approved purchases make a shop familiar. The compiler asks the
# customer this; until they answer, one prior purchase is the reading that does
# not invent a relationship the history does not show.
DEFAULT_FAMILIARITY_THRESHOLD = 1

# Attempts inside ten minutes that read as a burst rather than shopping.
VELOCITY_BURST = 3


def tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in STOPWORDS}


@dataclass(frozen=True)
class Resolved:
    """A resolved field value, or an honest admission that it is unknown."""

    value: Any
    detail: str
    established: bool = True

    @classmethod
    def unknown(cls, detail: str) -> Resolved:
        return cls(None, detail, established=False)


@dataclass
class Facts:
    """Everything the rules and signals stages read from."""

    event: AuthorizationEvent
    state: RunState
    history: CardHistory
    familiarity_threshold: int = DEFAULT_FAMILIARITY_THRESHOLD

    line_text: dict[int, Sanitised] = dc_field(default_factory=dict, init=False)
    order_text: Sanitised = dc_field(init=False)

    def __post_init__(self) -> None:
        auth = self.event.authorization
        self.line_text = {item.line_no: sanitise(item.item_details) for item in auth.items}
        self.order_text = sanitise(auth.purchase_description)

    # --- merchant-supplied text -------------------------------------------

    @property
    def manipulated_lines(self) -> list[tuple[Item, Sanitised]]:
        auth = self.event.authorization
        return [
            (item, self.line_text[item.line_no])
            for item in auth.items
            if self.line_text[item.line_no].is_manipulated
        ]

    @property
    def any_text_manipulated(self) -> bool:
        return bool(self.manipulated_lines) or self.order_text.is_manipulated

    # --- familiarity -------------------------------------------------------

    @property
    def merchant_prior_purchases(self) -> int:
        auth = self.event.authorization
        return self.history.merchant_purchases(auth.card_id, auth.merchant.merchant_id)

    @property
    def device_prior_purchases(self) -> int:
        auth = self.event.authorization
        return self.history.device_purchases(auth.card_id, auth.customer_device_id)

    @property
    def lookalike(self) -> Lookalike | None:
        auth = self.event.authorization
        return self.history.find_lookalike(
            auth.card_id, auth.merchant.merchant_id, auth.merchant.merchant_name
        )

    def merchant_familiarity(self) -> Resolved:
        count = self.merchant_prior_purchases
        name = self.event.authorization.merchant.merchant_name
        if count >= self.familiarity_threshold:
            return Resolved("familiar", f"{count} earlier approved purchase(s) at {name}")
        impostor = self.lookalike
        if impostor:
            return Resolved(
                "unfamiliar",
                f"no earlier purchase at {name}, whose name closely resembles "
                f"{impostor.familiar_name}, a shop you do use "
                f"({impostor.similarity:.0%} similar)",
            )
        return Resolved("unfamiliar", f"no earlier approved purchase at {name}")

    # --- session -----------------------------------------------------------

    def session_signals(self) -> list[str]:
        auth = self.event.authorization
        signals: list[str] = []
        if not auth.customer_device_id:
            signals.append("no device was identified for this purchase")
        elif self.device_prior_purchases == 0:
            signals.append(
                f"the purchase came from {auth.customer_device_id}, a device this "
                "card has not used before"
            )
        if auth.recent_attempt_count_10m >= VELOCITY_BURST:
            signals.append(
                f"{auth.recent_attempt_count_10m} attempts in the last ten minutes, "
                "which reads as a burst rather than shopping"
            )
        return signals

    def session_integrity(self) -> Resolved:
        auth = self.event.authorization
        if not auth.customer_device_id:
            return Resolved.unknown("no device was identified, so the session cannot be judged")
        signals = self.session_signals()
        if signals:
            return Resolved("degraded", "; ".join(signals))
        return Resolved(
            "normal",
            f"{auth.customer_device_id} is a device this card uses "
            f"({self.device_prior_purchases} earlier purchases)",
        )

    # --- order terms -------------------------------------------------------

    def return_window_days(self) -> Resolved:
        """The order is returnable only for as long as its worst line is.

        A basket is bought as one order, so the shortest window on any line
        governs the whole thing.
        """
        windows: list[tuple[int, int]] = []
        refused: list[int] = []
        unstated: list[int] = []
        untrusted: list[int] = []

        for item in self.event.authorization.items:
            text = self.line_text[item.line_no]
            if not text.trustworthy_for_facts:
                untrusted.append(item.line_no)
                continue
            if text.facts.return_explicitly_refused:
                refused.append(item.line_no)
            elif text.facts.return_window_days is not None:
                windows.append((text.facts.return_window_days, item.line_no))
            else:
                unstated.append(item.line_no)

        if refused:
            return Resolved(0, f"line {refused[0]} is sold as a final sale, with no returns")
        if untrusted:
            return Resolved.unknown(
                f"line {untrusted[0]}'s product text tries to steer this decision, so its "
                "stated return terms cannot be relied on"
            )
        if unstated and not windows:
            return Resolved.unknown("the seller did not state a return window")
        if unstated:
            return Resolved.unknown(
                f"line {unstated[0]} states no return window, so the order's terms are "
                "not established even though other lines state one"
            )
        shortest = min(windows)
        return Resolved(shortest[0], f"returns accepted within {shortest[0]} days")

    # --- what was asked for ------------------------------------------------

    def _requested_phrase(self) -> str | None:
        for rule in self.event.mandate.hard_rules:
            if rule.field == "derived.requested_item" and isinstance(rule.value, str):
                return rule.value
        return None

    def _line_tokens(self, item: Item) -> set[str]:
        """Tokens identifying what a cart line *is*.

        Deliberately the item name and category only. Product copy is not
        identity: a trail shoe described as having a "lugged off-road sole"
        would otherwise contain the word "road" and pass as the road-running
        shoe the customer asked for. Prose is read for attributes such as size
        elsewhere, where a stray word cannot change what the thing is.
        """
        return tokens(item.item_name) | tokens(item.item_category)

    def matching_lines(self) -> list[Item]:
        phrase = self._requested_phrase()
        if not phrase:
            return list(self.event.authorization.items)
        wanted = tokens(phrase)
        return [i for i in self.event.authorization.items if wanted <= self._line_tokens(i)]

    def unrequested_lines(self) -> list[Item]:
        if not self._requested_phrase():
            return []
        matched = {i.line_no for i in self.matching_lines()}
        return [i for i in self.event.authorization.items if i.line_no not in matched]

    def requested_item_match(self) -> Resolved:
        phrase = self._requested_phrase()
        if not phrase:
            return Resolved.unknown("the instruction did not name a particular item")
        matched = self.matching_lines()
        if matched:
            return Resolved("match", f'line {matched[0].line_no} is the "{phrase}" you asked for')
        if self.any_text_manipulated:
            return Resolved.unknown(
                f'no line is recognisably the "{phrase}" you asked for, and the seller\'s '
                "product text cannot be trusted to describe it"
            )
        names = ", ".join(i.item_name for i in self.event.authorization.items)
        return Resolved("mismatch", f'you asked for "{phrase}"; the order contains {names}')

    def requested_attribute(self, wanted: str) -> Resolved:
        """Check a stated attribute such as `size=43` or `inch=27`."""
        kind, _, value = wanted.partition("=")
        kind, value = kind.strip().lower(), value.strip().lower()
        label = {"size": "size", "inch": "screen size in inches"}.get(kind, kind)
        seen: list[str] = []
        untrusted = False

        for item in self.matching_lines() or self.event.authorization.items:
            text = self.line_text[item.line_no]
            if not text.trustworthy_for_facts:
                untrusted = True
                continue
            found = text.facts.sizes if kind == "size" else text.facts.inches
            seen.extend(str(f).lower() for f in found)

        if value in seen:
            return Resolved(wanted, f"the seller states {label} {value}")
        if untrusted and not seen:
            return Resolved.unknown(
                f"the {label} could not be established: the product text tries to steer "
                "this decision"
            )
        if not seen:
            return Resolved.unknown(f"the seller does not state a {label}")
        return Resolved(f"{kind}={seen[0]}", f"the seller states {label} {seen[0]}, not {value}")

    # --- money -------------------------------------------------------------

    def spend_in_period(self, days: int) -> Resolved:
        """What the window total becomes if this order goes through.

        "Keep the total across any seven days at or below CHF 300" is a claim
        about the total *including* the order being decided. Comparing only the
        prior total would let the purchase that actually breaks the limit
        through and catch the next one instead.

        Prior spend counts only final approvals, on simulated purchase time.
        """
        auth = self.event.authorization
        prior = self.state.approved_spend_within(days, auth.timestamp)
        platform = self.event.context.approved_spend_in_period_chf
        source = f"CHF {prior} finally approved in the {days} days before this order"

        if platform is not None and Decimal(platform) > prior:
            # Never under-count a limit. If the platform has seen spend we have
            # not, its figure governs.
            prior = Decimal(platform)
            source = (
                f"CHF {platform} approved in this run according to the platform, "
                f"more than we had recorded"
            )

        total = prior + Decimal(auth.billing_amount_chf)
        detail = f"{source}; this order would take the {days}-day total to CHF {total}"
        pending = self.state.pending_spend_within(days, auth.timestamp)
        if pending:
            detail += f", with a further CHF {pending} waiting for your answer and not counted"
        return Resolved(total, detail)

    def item_values(self, field: str) -> list[tuple[Item, Any]]:
        auth = self.event.authorization
        if field == "item.item_category":
            return [(i, i.item_category) for i in auth.items]
        if field == "item.unit_price_chf":
            return [(i, to_chf(i.unit_price, i.currency)) for i in auth.items]
        raise KeyError(field)

    # --- plain event fields ------------------------------------------------

    def event_value(self, field: str) -> Resolved:
        auth = self.event.authorization
        simple = {
            "authorization.billing_amount_chf": auth.billing_amount_chf,
            "authorization.merchant.merchant_category": auth.merchant.merchant_category,
            "authorization.merchant.merchant_country": auth.merchant.merchant_country,
            "authorization.merchant.merchant_id": auth.merchant.merchant_id,
            "authorization.fulfillment_method": auth.fulfillment_method,
            "authorization.order_returnable": auth.order_returnable.value,
            "authorization.channel": auth.channel.value,
            "cart.line_count": len(auth.items),
            "cart.total_quantity": auth.cart_size,
        }
        if field not in simple:
            raise KeyError(field)
        value = simple[field]
        label = field.rsplit(".", 1)[-1].replace("_", " ")

        # "unknown" means the seller did not supply the term and
        # "not_applicable" means it does not apply. Neither is permission — and
        # neither is a refusal either. Both are things we could not establish.
        if field in ("authorization.order_returnable",) and value in (
            OrderTerm.UNKNOWN.value,
            OrderTerm.NOT_APPLICABLE.value,
        ):
            reason = (
                "the seller did not state whether this order can be returned"
                if value == OrderTerm.UNKNOWN.value
                else "returning does not apply to this kind of order"
            )
            return Resolved.unknown(reason)

        return Resolved(value, f"{label} is {value}")

    def resolve(self, field: str, rule_value: Any = None, period_days: int | None = None) -> Resolved:
        """Resolve any field in the vocabulary."""
        if field == "derived.merchant_familiarity":
            return self.merchant_familiarity()
        if field == "derived.session_integrity":
            return self.session_integrity()
        if field == "derived.return_window_days":
            return self.return_window_days()
        if field == "derived.requested_item_match":
            return self.requested_item_match()
        if field == "derived.requested_attribute":
            return self.requested_attribute(str(rule_value))
        if field == "derived.requested_item":
            phrase = self._requested_phrase()
            return Resolved(phrase, f'the instruction named "{phrase}"') if phrase else Resolved.unknown("no item named")
        if field == "derived.unrequested_line_count":
            extras = self.unrequested_lines()
            if extras:
                names = ", ".join(i.item_name for i in extras)
                return Resolved(len(extras), f"the order also contains {names}, which you did not ask for")
            return Resolved(0, "the order contains nothing you did not ask for")
        if field == "derived.spend_in_period_chf":
            return self.spend_in_period(period_days or 1)
        return self.event_value(field)


def compare(left: Any, operator: Operator, right: Any) -> bool | None:
    """Apply a rule operator.

    Returns None when the two values cannot be compared at all — a numeric
    operator against a non-numeric value, say. That is uncertainty, not a
    failure, and the caller must treat it as such: silently returning False
    here would turn "we could not tell" into "the customer forbade it".

    Numbers compare as Decimal so a rule never rides on binary float error.
    """
    if operator is Operator.IN:
        return str(left) in {str(v) for v in right} if isinstance(right, list) else str(left) == str(right)
    if operator is Operator.NOT_IN:
        return str(left) not in {str(v) for v in right} if isinstance(right, list) else str(left) != str(right)
    if operator is Operator.EQ:
        return str(left) == str(right)
    if operator is Operator.NE:
        return str(left) != str(right)

    try:
        lhs, rhs = Decimal(str(left)), Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return {
        Operator.LT: lhs < rhs,
        Operator.LTE: lhs <= rhs,
        Operator.GT: lhs > rhs,
        Operator.GTE: lhs >= rhs,
    }[operator]
