"""The field namespace a stored rule may address, and the words that reach it.

A rule's `field` is "a convention for your engine to interpret, not a formula
the API runs". That freedom is also a hazard: a field name nothing resolves is
a rule that silently never fires. So the namespace is closed and declared here,
every compiled rule is checked against it, and each field records whether it
reads a plain event field, every line of the cart, or something derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Resolution(str, Enum):
    """Where a field's value comes from."""

    EVENT = "event"
    """Read straight off the authorization."""

    EVERY_ITEM = "every_item"
    """Holds only if every cart line satisfies it. A basket passes as a whole."""

    ANY_ITEM = "any_item"
    """Holds if at least one cart line satisfies it."""

    DERIVED = "derived"
    """Computed by the engine from history, run state, or merchant text."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    resolution: Resolution
    kind: str  # "money" | "number" | "string" | "term"
    description: str


def _spec(name: str, resolution: Resolution, kind: str, description: str) -> FieldSpec:
    return FieldSpec(name, resolution, kind, description)


FIELDS: dict[str, FieldSpec] = {
    f.name: f
    for f in [
        _spec(
            "authorization.billing_amount_chf",
            Resolution.EVENT,
            "money",
            "Order total in CHF. Already includes delivery; never add the fee again.",
        ),
        _spec(
            "authorization.merchant.merchant_category",
            Resolution.EVENT,
            "string",
            "What kind of shop this is. Says nothing about what is in the basket.",
        ),
        _spec(
            "authorization.merchant.merchant_country",
            Resolution.EVENT,
            "string",
            "Merchant's country. Not a substitute for the row's currency.",
        ),
        _spec(
            "authorization.merchant.merchant_id",
            Resolution.EVENT,
            "string",
            "Exact merchant. The only merchant field a lookalike name cannot forge.",
        ),
        _spec(
            "authorization.fulfillment_method",
            Resolution.EVENT,
            "string",
            "How the order is fulfilled, e.g. delivery or collection.",
        ),
        _spec(
            "authorization.order_returnable",
            Resolution.EVENT,
            "term",
            "Whether the order can be returned: true, false, unknown, not_applicable.",
        ),
        _spec(
            "authorization.channel",
            Resolution.EVENT,
            "string",
            "Payment rail for this purchase.",
        ),
        _spec(
            "item.item_category",
            Resolution.EVERY_ITEM,
            "string",
            "Category of a cart line. Item-only categories such as cosmetics and "
            "gift_card have no merchant counterpart: a supermarket sells cosmetics "
            "without ceasing to be a grocery merchant.",
        ),
        _spec(
            "item.unit_price_chf",
            Resolution.EVERY_ITEM,
            "money",
            "One cart line's unit price converted at the line's own currency.",
        ),
        _spec(
            "cart.line_count",
            Resolution.DERIVED,
            "number",
            "Number of distinct lines in the basket.",
        ),
        _spec(
            "cart.total_quantity",
            Resolution.DERIVED,
            "number",
            "Total units across the basket.",
        ),
        _spec(
            "derived.spend_in_period_chf",
            Resolution.DERIVED,
            "money",
            "Finally approved spend inside the rolling window, from run state. "
            "A purchase awaiting a human answer is not yet spend.",
        ),
        _spec(
            "derived.return_window_days",
            Resolution.DERIVED,
            "number",
            "Return window in days, read out of merchant text. Absent when the "
            "seller did not state one — which is not the same as zero.",
        ),
        _spec(
            "derived.merchant_familiarity",
            Resolution.DERIVED,
            "string",
            "Whether the cardholder has used this exact merchant before: "
            "familiar, unfamiliar, or unknown.",
        ),
        _spec(
            "derived.requested_item_match",
            Resolution.DERIVED,
            "string",
            "Does the basket contain what the customer actually asked for: "
            "match, mismatch, or uncertain.",
        ),
        _spec(
            "derived.requested_item",
            Resolution.DERIVED,
            "string",
            "The thing the customer described, in their words. The engine matches "
            "it against the cart; a trail shoe is not a road shoe and a voucher is "
            "not a monitor.",
        ),
        _spec(
            "derived.requested_attribute",
            Resolution.DERIVED,
            "string",
            "A stated attribute of the requested item, such as a size, that the "
            "engine looks for in merchant text.",
        ),
        _spec(
            "derived.unrequested_line_count",
            Resolution.DERIVED,
            "number",
            "Cart lines the customer did not ask for, such as add-on cover.",
        ),
        _spec(
            "derived.session_integrity",
            Resolution.DERIVED,
            "string",
            "Session signal from device novelty, velocity and country: "
            "normal, degraded, or unknown.",
        ),
    ]
}


def is_known(field: str) -> bool:
    return field in FIELDS


# --- lexicon ---------------------------------------------------------------
#
# Maps the customer's words onto the category vocabulary the data actually
# uses. Kept explicit rather than inferred, because a wrong guess here silently
# widens or narrows what the customer authorised.

MERCHANT_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "grocery": ("groceries",),
    "groceries": ("groceries",),
    "supermarket": ("groceries",),
    "clothing": ("clothing",),
    "clothes": ("clothing",),
    "apparel": ("clothing",),
    "electronics": ("electronics",),
    "sports": ("sporting_goods",),
    "sport": ("sporting_goods",),
    "sporting": ("sporting_goods",),
    "running": ("sporting_goods",),
    "books": ("books",),
    "pharmacy": ("health",),
    "health": ("health",),
    "pet": ("pet_care",),
    "hotel": ("hotel",),
    "travel": ("travel",),
    "software": ("software",),
    "household": ("household",),
}

ITEM_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    **MERCHANT_CATEGORY_WORDS,
    "cosmetics": ("cosmetics",),
    "beauty": ("cosmetics",),
    "fragrance": ("cosmetics",),
    "gift card": ("gift_card",),
    "gift cards": ("gift_card",),
    "gift voucher": ("gift_card",),
    "vouchers": ("gift_card",),
    "voucher": ("gift_card",),
    "membership": ("membership",),
    "subscription": ("subscriptions",),
    "subscriptions": ("subscriptions",),
}

NUMBER_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "twenty": 20, "thirty": 30, "sixty": 60, "ninety": 90,
}

DAYS_PER_UNIT: dict[str, int] = {
    "day": 1, "days": 1,
    "week": 7, "weeks": 7,
    "fortnight": 14,
    "month": 30, "months": 30,
    "year": 365, "years": 365,
}
