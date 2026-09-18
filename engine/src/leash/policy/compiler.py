"""Turn the customer's own words into checks they can confirm.

The output is three things, and the third matters as much as the first:

* `hard_rules` — executable checks, in the API's stored rule format.
* `guidance` — what the system understood, in plain language, so the customer
  can tell whether we heard them correctly before anything is spent.
* `open_questions` — what the instruction does not settle. "A shop I use
  regularly" has no field in the event; somebody has to decide what "regularly"
  means, and that somebody is the customer, not us. Quietly picking a threshold
  here would be the whole failure this challenge is about.

Extraction is deterministic pattern matching over the instruction. No model is
involved: policy compilation happens once, before the customer confirms, and it
must produce the same rules every time it reads the same sentence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal

from ..models.enums import Currency, Operator, RuleScope, UncertaintyPolicy
from .vocabulary import (
    DAYS_PER_UNIT,
    FIELDS,
    ITEM_CATEGORY_WORDS,
    MERCHANT_CATEGORY_WORDS,
    NUMBER_WORDS,
)

CURRENCIES = "|".join(c.value for c in Currency)

# "CHF 200", "CHF 20.50", "200 CHF"
MONEY = re.compile(
    rf"(?:(?P<cur1>{CURRENCIES})\s*(?P<amt1>\d+(?:[.,]\d+)?)"
    rf"|(?P<amt2>\d+(?:[.,]\d+)?)\s*(?P<cur2>{CURRENCIES}))",
    re.IGNORECASE,
)

_NUM = r"(?P<count>\d+|" + "|".join(NUMBER_WORDS) + r")"
_UNIT = r"(?P<unit>" + "|".join(DAYS_PER_UNIT) + r")"

# "across any seven days", "over 7 days", "in any 30 days", "per week"
PERIOD = re.compile(rf"(?:across|over|within|in|per|every)\s+(?:any\s+)?{_NUM}?\s*{_UNIT}", re.IGNORECASE)

# "returned within 14 days or more", "returnable for at least 30 days"
RETURN_WINDOW = re.compile(
    rf"return(?:ed|able|s)?\b[^.;]*?{_NUM}\s*{_UNIT}(?P<orMore>\s*or\s+more|\s*or\s+longer)?",
    re.IGNORECASE,
)

AT_MOST = re.compile(
    r"or\s+less|or\s+below|no\s+more\s+than|not?\s+more\s+than|up\s+to|at\s+or\s+below"
    r"|below|under|maximum|max\b|at\s+most|cap(?:ped)?\s+at|not\s+exceed",
    re.IGNORECASE,
)
AT_LEAST = re.compile(r"or\s+more|or\s+longer|at\s+least|no\s+less\s+than|minimum|min\b", re.IGNORECASE)

PER_PURCHASE = re.compile(r"each\s+order|per\s+order|per\s+purchase|any\s+one\s+order|each\s+purchase", re.IGNORECASE)

# "buy the 27-inch monitor I chose", "replace my worn road-running shoes",
# "the agent may buy clothing for me". First match wins; order is specificity.
REQUESTED_ITEM = (
    re.compile(r"\breplace\s+my\s+(?:worn\s+|old\s+|current\s+)?(?P<what>[\w\s-]+?)(?=\s+in\s+size\b|\s+for\b|[,.;]|$)", re.IGNORECASE),
    re.compile(r"\bbuy\s+(?:me\s+)?the\s+(?P<what>[\w\s-]+?)(?=\s+I\s+(?:chose|picked|asked|want)|\s+for\b|[,.;]|$)", re.IGNORECASE),
    re.compile(r"\bmay\s+buy\s+(?P<what>[\w\s-]+?)(?=\s+for\s+me\b|\s+for\b|[,.;]|$)", re.IGNORECASE),
    re.compile(
        r"\bbuy\s+(?:me\s+|us\s+)?(?:one\s+|a\s+|an\s+)?(?:ordinary\s+)?"
        r"(?P<what>[\w\s-]+?)(?=\s+for\b|\s+from\b|\s+at\b|\s+under\b|[,.;]|$)",
        re.IGNORECASE,
    ),
)

# Words that name a kind of thing rather than a particular thing. "One ordinary
# grocery item" describes a category, which the item_category rule already
# covers; matching a cart line against the literal phrase "grocery item" would
# reject a basket of fresh produce for not being called that.
GENERIC_ITEM_WORDS = frozenset(
    {
        "item", "items", "order", "orders", "thing", "things", "stuff",
        "goods", "product", "products", "purchase", "purchases", "shopping",
        "ordinary", "usual", "regular", "some", "any", "my", "our", "the",
    }
)

SIZE = re.compile(r"\bsize\s+(?P<size>[\w-]+)", re.IGNORECASE)
INCH = re.compile(r"(?P<inch>\d+)[\s-]*(?:inch|\"|in\b)", re.IGNORECASE)

FAMILIAR = re.compile(
    r"shops?\s+I\s+(?:have\s+)?(?:used|use|bought|shopped)"
    r"|sellers?\s+I\s+(?:have\s+)?(?:used|use|bought)"
    r"|(?:shop|seller|merchant|retailer)s?\s+I\s+(?:use|used|know)"
    r"|I\s+(?:have\s+)?(?:used|bought\s+from)\s+before"
    r"|familiar\s+(?:shop|seller|merchant)s?"
    r"|regular(?:ly)?",
    re.IGNORECASE,
)
SPECIALIST = re.compile(r"specialist\s+(?P<what>[\w\s]+?)\s+(?:retailer|shop|store|seller)", re.IGNORECASE)
NO_EXTRAS = re.compile(
    r"(?:do\s+not|don't|no)\s+add(?:ing)?\s+anything|nothing\s+I\s+did\s*n[o']t\s+ask"
    r"|only\s+what\s+I\s+asked|no\s+extras|no\s+add-?ons",
    re.IGNORECASE,
)
SESSION = re.compile(
    r"someone\s+other\s+than\s+me|not\s+me\s+driving|hijack|taken\s+over"
    r"|looks\s+like\s+.{0,30}\bsession\b|unusual\s+session",
    re.IGNORECASE,
)
UNCERTAIN_ASK = re.compile(r"ask\s+me|check\s+with\s+me|confirm\s+with\s+me|pause\s+and\s+ask", re.IGNORECASE)
UNCERTAIN_DECLINE = re.compile(r"(?:decline|refuse|block|reject)\s+(?:if|when|anything)\s+.{0,20}(?:uncertain|unsure|doubt)", re.IGNORECASE)

DELIVERY = re.compile(r"\bfor\s+delivery\b|\bdelivered\b|\bdelivery\b", re.IGNORECASE)


@dataclass(frozen=True)
class CompiledRule:
    """One stored check, in the API's rule format."""

    field: str
    operator: Operator
    value: int | float | str | list[str]
    currency: Currency | None = None
    scope: RuleScope | None = None
    period_days: int | None = None
    # Not sent to the API — kept so the UI can show which words produced this.
    source: str = ""

    def to_payload(self) -> dict:
        """The wire form. Optional fields are omitted, not sent as null."""
        payload: dict = {
            "field": self.field,
            "operator": self.operator.value,
            "value": self.value,
        }
        if self.currency is not None:
            payload["currency"] = self.currency.value
        if self.scope is not None:
            payload["scope"] = self.scope.value
        if self.period_days is not None:
            payload["period_days"] = self.period_days
        return payload

    def identity(self) -> tuple:
        """What makes two rules the same check, ignoring where they came from."""
        value = tuple(self.value) if isinstance(self.value, list) else self.value
        return (self.field, self.operator, value, self.currency, self.scope, self.period_days)


@dataclass
class CompiledPolicy:
    instruction: str
    hard_rules: list[CompiledRule] = dc_field(default_factory=list)
    guidance: list[str] = dc_field(default_factory=list)
    open_questions: list[str] = dc_field(default_factory=list)
    uncertainty_policy: UncertaintyPolicy = UncertaintyPolicy.ASK

    def to_draft_payload(self) -> dict:
        """Body for `POST /v1/mandates`.

        The instruction goes out in the customer's exact original wording.
        """
        return {
            "instruction": self.instruction,
            "hard_rules": [r.to_payload() for r in self.hard_rules],
            "uncertainty_policy": self.uncertainty_policy.value,
            "guidance": list(self.guidance),
            "open_questions": list(self.open_questions),
        }


# --- helpers ---------------------------------------------------------------


def _number(token: str | None) -> int | None:
    if not token:
        return None
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def _decimal(token: str) -> Decimal:
    return Decimal(token.replace(",", "."))


def _clauses(text: str) -> list[str]:
    """Split an instruction into the spans a single check can be read from.

    Splitting on connectives keeps "CHF 120 per order" and "CHF 300 across
    seven days" from contaminating each other's scope.
    """
    parts = re.split(r"[,;.]| and | but ", text)
    return [p.strip() for p in parts if p.strip()]


def _category_words(text: str, lexicon: dict[str, tuple[str, ...]]) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    for word, categories in lexicon.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            for category in categories:
                if category not in found:
                    found.append(category)
    return found


# --- extractors ------------------------------------------------------------
#
# Each returns rules, guidance lines and open questions for one aspect of the
# instruction. They are independent so a sentence that says nothing about, say,
# return terms simply produces nothing rather than a default.


def _extract_amounts(text: str) -> tuple[list[CompiledRule], list[str], list[str]]:
    rules: list[CompiledRule] = []
    guidance: list[str] = []
    questions: list[str] = []

    for clause in _clauses(text):
        for match in MONEY.finditer(clause):
            raw = match.group("amt1") or match.group("amt2")
            currency = Currency((match.group("cur1") or match.group("cur2")).upper())
            amount = _decimal(raw)

            if AT_LEAST.search(clause) and not AT_MOST.search(clause):
                # A spending floor is not something an instruction normally
                # means; surface it rather than inverting the customer's cap.
                questions.append(
                    f'"{clause.strip()}" reads as a minimum of {currency.value} {amount}. '
                    "Did you mean that as a limit instead?"
                )
                continue

            period = PERIOD.search(clause)
            days = None
            if period:
                unit_days = DAYS_PER_UNIT[period.group("unit").lower()]
                count = _number(period.group("count"))
                days = unit_days * count if count else unit_days

            if days:
                rules.append(
                    CompiledRule(
                        field="derived.spend_in_period_chf",
                        operator=Operator.LTE,
                        value=float(amount),
                        currency=currency,
                        scope=RuleScope.PERIOD,
                        period_days=days,
                        source=clause,
                    )
                )
                guidance.append(
                    f"Total finally approved spend stays at or below "
                    f"{currency.value} {amount} across any rolling {days} days. "
                    "A purchase still waiting for your answer is not counted as spent."
                )
            else:
                rules.append(
                    CompiledRule(
                        field="authorization.billing_amount_chf",
                        operator=Operator.LTE,
                        value=float(amount),
                        currency=currency,
                        scope=RuleScope.PURCHASE,
                        source=clause,
                    )
                )
                note = f"Each order stays at or below {currency.value} {amount}."
                if re.search(r"including\s+delivery|incl\.?\s+delivery", clause, re.IGNORECASE):
                    note += " The order total already includes the delivery fee."
                guidance.append(note)
                questions.append(
                    "Two orders placed minutes apart at the same shop can each stay "
                    f"under {currency.value} {amount} while together going over it. "
                    "Should closely spaced orders from one shop be treated as a "
                    "single order, or brought to you?"
                )

            if currency is not Currency.CHF:
                questions.append(
                    f"You gave the limit in {currency.value}. Purchases are "
                    "evaluated in CHF at fixed rates. Is converting to CHF correct?"
                )
    return rules, guidance, questions


def _extract_categories(text: str) -> tuple[list[CompiledRule], list[str], list[str]]:
    rules: list[CompiledRule] = []
    guidance: list[str] = []
    questions: list[str] = []

    specialist = SPECIALIST.search(text)
    if specialist:
        what = specialist.group("what").strip()
        categories = _category_words(what, MERCHANT_CATEGORY_WORDS)
        if categories:
            rules.append(
                CompiledRule(
                    field="authorization.merchant.merchant_category",
                    operator=Operator.IN,
                    value=list(categories),
                    source=specialist.group(0),
                )
            )
            guidance.append(
                f"Only shops categorised as {', '.join(categories)} count as a "
                f'specialist "{what}" retailer.'
            )
        questions.append(
            f'You asked to buy only from a specialist "{what}" retailer. We read '
            f"that as the shop category {categories or ['(nothing matched)']}. Should "
            "a general retailer that also sells these count, or only a specialist?"
        )
        return rules, guidance, questions

    # A category named for the *purpose* of the order constrains the basket,
    # not the shop. A supermarket sells cosmetics without ceasing to be a
    # grocery merchant, so this rule goes on the cart lines.
    categories = _category_words(text, ITEM_CATEGORY_WORDS)
    if categories:
        rules.append(
            CompiledRule(
                field="item.item_category",
                operator=Operator.IN,
                value=list(categories),
                source=text,
            )
        )
        guidance.append(
            f"Every line in the basket must be one of: {', '.join(categories)}. "
            "The shop's own category does not vouch for what is in the basket."
        )
    return rules, guidance, questions


def _extract_return_terms(text: str) -> tuple[list[CompiledRule], list[str], list[str]]:
    match = RETURN_WINDOW.search(text)
    if not match:
        return [], [], []
    count = _number(match.group("count"))
    if count is None:
        return [], [], []
    days = count * DAYS_PER_UNIT[match.group("unit").lower()]
    rules = [
        CompiledRule(
            field="derived.return_window_days",
            operator=Operator.GTE,
            value=days,
            source=match.group(0),
        ),
        CompiledRule(
            field="authorization.order_returnable",
            operator=Operator.EQ,
            value="true",
            source=match.group(0),
        ),
    ]
    guidance = [
        (
            f"The order must be returnable for at least {days} days, and the seller "
            "must actually say so. A seller who states no return policy has not met "
            "this condition."
        )
    ]
    questions = [
        (
            "If a seller does not state a return window at all, should that be "
            "refused outright, or brought to you?"
        )
    ]
    return rules, guidance, questions


def _extract_merchant_familiarity(text: str) -> tuple[list[CompiledRule], list[str], list[str]]:
    if not FAMILIAR.search(text):
        return [], [], []
    rules = [
        CompiledRule(
            field="derived.merchant_familiarity",
            operator=Operator.EQ,
            value="familiar",
            source=FAMILIAR.search(text).group(0),
        )
    ]
    guidance = [
        (
            "The shop must be one this card has paid before, matched on the shop's "
            "identifier rather than its name. A new shop with a near-identical name "
            "is treated as a different shop."
        )
    ]
    questions = [
        "How many past purchases make a shop familiar to you — one, or a few?",
        (
            "If a shop is new but otherwise meets every condition, should that be "
            "refused, or brought to you?"
        ),
    ]
    return rules, guidance, questions


def _extract_requested_item(text: str) -> tuple[list[CompiledRule], list[str], list[str]]:
    rules: list[CompiledRule] = []
    guidance: list[str] = []
    questions: list[str] = []

    for pattern in REQUESTED_ITEM:
        match = pattern.search(text)
        if not match:
            continue
        what = " ".join(match.group("what").split()).strip().lower()
        # Strip leading filler the patterns can pick up before the noun.
        what = re.sub(r"^(?:me|us|my|our|the|some)\s+", "", what)
        tokens = {t for t in re.split(r"[\s-]+", what) if t}
        generic = GENERIC_ITEM_WORDS | set(ITEM_CATEGORY_WORDS)
        if tokens and tokens <= generic:
            # Nothing specific was named. The category rule already covers it.
            break
        if what and len(what) <= 60:
            rules.append(
                CompiledRule(
                    field="derived.requested_item",
                    operator=Operator.EQ,
                    value=what,
                    source=match.group(0).strip(),
                )
            )
            guidance.append(
                f'You asked for "{what}". The basket must actually contain that, '
                "judged on the seller's product text rather than the order title."
            )
        break

    size = SIZE.search(text)
    if size:
        value = size.group("size")
        rules.append(
            CompiledRule(
                field="derived.requested_attribute",
                operator=Operator.EQ,
                value=f"size={value}",
                source=size.group(0),
            )
        )
        guidance.append(
            f"The item must be size {value}. The size is read from the seller's "
            "product text; a different size is a different item."
        )
    inch = INCH.search(text)
    if inch:
        value = inch.group("inch")
        rules.append(
            CompiledRule(
                field="derived.requested_attribute",
                operator=Operator.EQ,
                value=f"inch={value}",
                source=inch.group(0),
            )
        )
        guidance.append(f"The item must be the {value}-inch version.")

    if NO_EXTRAS.search(text):
        rules.append(
            CompiledRule(
                field="derived.unrequested_line_count",
                operator=Operator.EQ,
                value=0,
                source=NO_EXTRAS.search(text).group(0),
            )
        )
        guidance.append(
            "Nothing may be added that you did not ask for — protection plans, "
            "accessories or vouchers bundled into the same order."
        )

    if rules:
        rules.append(
            CompiledRule(
                field="derived.requested_item_match",
                operator=Operator.EQ,
                value="match",
                source="the item you described",
            )
        )
        questions.append(
            "If the shop offers a close substitute rather than exactly what you "
            "described, should that be refused, or brought to you?"
        )
    return rules, guidance, questions


def _extract_quantity(text: str) -> tuple[list[CompiledRule], list[str], list[str]]:
    match = re.search(r"\bbuy\s+(?P<count>one|a|an|\d+)\s+(?!of\b)", text, re.IGNORECASE)
    if not match:
        return [], [], []
    token = match.group("count").lower()
    count = 1 if token in {"a", "an"} else _number(token)
    if count is None:
        return [], [], []
    rules = [
        CompiledRule(
            field="cart.total_quantity",
            operator=Operator.LTE,
            value=count,
            source=match.group(0).strip(),
        )
    ]
    return rules, [f"At most {count} unit(s) in the order."], []


def _extract_fulfillment(text: str) -> tuple[list[CompiledRule], list[str], list[str]]:
    if not DELIVERY.search(text):
        return [], [], []
    rules = [
        CompiledRule(
            field="authorization.fulfillment_method",
            operator=Operator.EQ,
            value="delivery",
            source=DELIVERY.search(text).group(0),
        )
    ]
    return rules, ["The order is fulfilled by delivery."], []


def _extract_session(text: str) -> tuple[list[CompiledRule], list[str], list[str]]:
    if not SESSION.search(text):
        return [], [], []
    rules = [
        CompiledRule(
            field="derived.session_integrity",
            operator=Operator.NE,
            value="degraded",
            source=SESSION.search(text).group(0),
        )
    ]
    guidance = [
        (
            "Purchases are paused when the session stops looking like you — an "
            "unfamiliar device, a burst of attempts, or an unusual country. When "
            "the signals return to normal, ordinary shopping resumes."
        )
    ]
    questions = [
        (
            "A device you have not used before is the strongest single signal here. "
            "Should a new device always be brought to you, even for a small purchase?"
        )
    ]
    return rules, guidance, questions


EXTRACTORS = (
    _extract_amounts,
    _extract_categories,
    _extract_return_terms,
    _extract_merchant_familiarity,
    _extract_requested_item,
    _extract_quantity,
    _extract_fulfillment,
    _extract_session,
)


def _uncertainty_policy(text: str) -> UncertaintyPolicy:
    if UNCERTAIN_DECLINE.search(text):
        return UncertaintyPolicy.DECLINE
    if UNCERTAIN_ASK.search(text):
        return UncertaintyPolicy.ASK
    # No stated preference. Asking is the conservative reading: it neither
    # spends the customer's money nor blocks their shopping on our guess.
    return UncertaintyPolicy.ASK


def compile_policy(instruction: str) -> CompiledPolicy:
    """Compile a natural-language instruction into a confirmable policy."""
    text = instruction.strip()
    policy = CompiledPolicy(instruction=instruction, uncertainty_policy=_uncertainty_policy(text))

    seen: set[tuple] = set()
    for extractor in EXTRACTORS:
        rules, guidance, questions = extractor(text)
        for rule in rules:
            if rule.field not in FIELDS:
                raise ValueError(f"extractor produced unknown field {rule.field!r}")
            if rule.identity() in seen:
                continue
            seen.add(rule.identity())
            policy.hard_rules.append(rule)
        policy.guidance.extend(guidance)
        policy.open_questions.extend(questions)

    if not policy.hard_rules:
        policy.open_questions.insert(
            0,
            "We could not turn this into any executable check. Please state a "
            "spending limit and what may be bought before authorising anything.",
        )

    if UNCERTAIN_ASK.search(text):
        policy.guidance.append("Anything we cannot settle is brought to you rather than guessed.")

    # De-duplicate while keeping the order the customer's words produced.
    policy.guidance = list(dict.fromkeys(policy.guidance))
    policy.open_questions = list(dict.fromkeys(policy.open_questions))
    return policy
