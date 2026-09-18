"""Evaluate the customer's confirmed rules against the resolved facts.

Rules combine with AND: a purchase must satisfy every one of them. That is what
makes the policy safe to add to — an extra rule can only ever remove purchases
from the allowed set.

A rule has three possible outcomes, and keeping the third distinct is the whole
point. `PASS` and `FAIL` are claims about the purchase. `UNCERTAIN` is a claim
about us: we could not establish the fact. Collapsing it into either of the
others is how a control layer either blocks ordinary shopping or waves through
something nobody checked.
"""

from __future__ import annotations

from ..models.events import MandateRule
from ..policy.vocabulary import FIELDS, Resolution
from .evidence import Finding, Outcome, Stage
from .facts import Facts, compare


def _describe(rule: MandateRule) -> str:
    value = ", ".join(rule.value) if isinstance(rule.value, list) else rule.value
    window = f" over any {rule.period_days} days" if rule.period_days else ""
    return f"{rule.field} {rule.operator.value} {value}{window}"


def evaluate_rule(rule: MandateRule, facts: Facts) -> Finding:
    """Evaluate one stored rule."""
    spec = FIELDS.get(rule.field)
    if spec is None:
        # A rule we cannot interpret must not be silently satisfied.
        return Finding(
            Stage.HARD_RULES,
            "rule_not_understood",
            Outcome.UNCERTAIN,
            f"your policy contains a check we do not know how to apply: {_describe(rule)}",
            field=rule.field,
        )

    if spec.resolution in (Resolution.EVERY_ITEM, Resolution.ANY_ITEM):
        return _evaluate_over_items(rule, facts, spec.resolution)

    resolved = facts.resolve(rule.field, rule.value, rule.period_days)
    if not resolved.established:
        return Finding(
            Stage.HARD_RULES,
            "fact_not_established",
            Outcome.UNCERTAIN,
            resolved.detail,
            field=rule.field,
            expected=_describe(rule),
        )

    verdict = compare(resolved.value, rule.operator, rule.value)
    if verdict is None:
        return Finding(
            Stage.HARD_RULES,
            "fact_not_comparable",
            Outcome.UNCERTAIN,
            f"{resolved.detail}, which cannot be checked against {_describe(rule)}",
            field=rule.field,
            observed=str(resolved.value),
            expected=_describe(rule),
        )

    if verdict:
        return Finding(
            Stage.HARD_RULES,
            "rule_satisfied",
            Outcome.PASS,
            resolved.detail,
            field=rule.field,
            observed=str(resolved.value),
            expected=_describe(rule),
        )

    # Some checks rest on an inference rather than an established fact. Breaching
    # one is a reason to ask, not a reason to refuse.
    escalates = spec.escalates_on_breach
    return Finding(
        Stage.HARD_RULES,
        "signal_breached" if escalates else "rule_breached",
        Outcome.UNCERTAIN if escalates else Outcome.FAIL,
        resolved.detail,
        field=rule.field,
        observed=str(resolved.value),
        expected=_describe(rule),
    )


def _evaluate_over_items(rule: MandateRule, facts: Facts, resolution: Resolution) -> Finding:
    """Apply a rule to the cart.

    `EVERY_ITEM` holds only if every line satisfies it. A basket is bought as
    one order, so a single line outside what the customer allowed breaches the
    rule — the shop's own category does not vouch for what is in the basket.
    """
    results = []
    for item, value in facts.item_values(rule.field):
        verdict = compare(value, rule.operator, rule.value)
        results.append((item, value, verdict))

    unknown = [r for r in results if r[2] is None]
    if unknown:
        item, value, _ = unknown[0]
        return Finding(
            Stage.HARD_RULES,
            "fact_not_comparable",
            Outcome.UNCERTAIN,
            f'line {item.line_no} ("{item.item_name}") gives {value}, which cannot be '
            f"checked against {_describe(rule)}",
            field=rule.field,
            expected=_describe(rule),
        )

    failing = [r for r in results if r[2] is False]
    if resolution is Resolution.EVERY_ITEM and failing:
        item, value, _ = failing[0]
        detail = f'line {item.line_no}, "{item.item_name}", is {value}'
        if len(failing) > 1:
            detail += f" (and {len(failing) - 1} other line(s) like it)"
        return Finding(
            Stage.HARD_RULES,
            "rule_breached",
            Outcome.FAIL,
            detail,
            field=rule.field,
            observed=str(value),
            expected=_describe(rule),
        )

    if resolution is Resolution.ANY_ITEM and len(failing) == len(results):
        return Finding(
            Stage.HARD_RULES,
            "rule_breached",
            Outcome.FAIL,
            f"no line in the order satisfies {_describe(rule)}",
            field=rule.field,
            expected=_describe(rule),
        )

    return Finding(
        Stage.HARD_RULES,
        "rule_satisfied",
        Outcome.PASS,
        f"every line satisfies {_describe(rule)}",
        field=rule.field,
        expected=_describe(rule),
    )
