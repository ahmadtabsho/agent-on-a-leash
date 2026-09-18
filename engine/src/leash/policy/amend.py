"""Changing a live policy may only ever tighten it.

The platform enforces part of this — a `PATCH` must keep every existing rule,
and `uncertainty_policy` may only move toward `decline` — but the brief asks
for more than the platform checks: "adding a rule must not weaken a customer's
existing restriction". So the real guarantee has to come from how rules
combine, not from the diff.

That guarantee is conjunction. Every hard rule is an AND, so a purchase must
satisfy all of them. Under conjunction an added rule can only ever remove
purchases from the allowed set, never add one back. A customer who adds
`<= CHF 200` on top of `<= CHF 120` does not get a CHF 200 budget; 120 still
binds. That is the correct outcome, but not the outcome they expected, so this
module tells them which rule actually governs rather than letting them believe
they raised their limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal

from ..models.enums import Operator, UncertaintyPolicy
from .compiler import CompiledRule

# Transitions the platform documents. `approve -> ask` is absent on purpose:
# the documented PATCH rules do not allow it, even though it is a tightening.
ALLOWED_POLICY_MOVES: set[tuple[UncertaintyPolicy, UncertaintyPolicy]] = {
    (UncertaintyPolicy.ASK, UncertaintyPolicy.ASK),
    (UncertaintyPolicy.DECLINE, UncertaintyPolicy.DECLINE),
    (UncertaintyPolicy.APPROVE, UncertaintyPolicy.APPROVE),
    (UncertaintyPolicy.ASK, UncertaintyPolicy.DECLINE),
    (UncertaintyPolicy.APPROVE, UncertaintyPolicy.DECLINE),
}

_NUMERIC = {Operator.LT, Operator.LTE, Operator.GT, Operator.GTE}
_UPPER_BOUND = {Operator.LT, Operator.LTE}
_LOWER_BOUND = {Operator.GT, Operator.GTE}


@dataclass
class AmendmentReview:
    """What a proposed change would actually do."""

    allowed: bool = True
    added: list[CompiledRule] = dc_field(default_factory=list)
    problems: list[str] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)

    def as_patch_payload(
        self, rules: list[CompiledRule], policy: UncertaintyPolicy | None
    ) -> dict:
        """Body for `PATCH /v1/mandates/{id}`. Omitted fields stay unchanged."""
        payload: dict = {"hard_rules": [r.to_payload() for r in rules]}
        if policy is not None:
            payload["uncertainty_policy"] = policy.value
        return payload


def _comparable(a: CompiledRule, b: CompiledRule) -> bool:
    """Do two rules constrain the same thing over the same window?"""
    return (a.field, a.scope, a.period_days) == (b.field, b.scope, b.period_days)


def _as_decimal(value) -> Decimal | None:
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return None


def binds_more_tightly(new: CompiledRule, existing: CompiledRule) -> bool | None:
    """Is `new` strictly tighter than `existing`?

    Returns None when the two are not comparable, which is not a failure — two
    rules on different fields simply both apply.
    """
    if not _comparable(new, existing):
        return None

    if new.operator in _NUMERIC and existing.operator in _NUMERIC:
        lhs, rhs = _as_decimal(new.value), _as_decimal(existing.value)
        if lhs is None or rhs is None:
            return None
        if new.operator in _UPPER_BOUND and existing.operator in _UPPER_BOUND:
            return lhs < rhs
        if new.operator in _LOWER_BOUND and existing.operator in _LOWER_BOUND:
            return lhs > rhs
        return None

    both_lists = isinstance(new.value, list) and isinstance(existing.value, list)
    if both_lists and new.operator is Operator.IN is existing.operator:
        # A narrower allow-list is tighter.
        return set(new.value) < set(existing.value)
    if both_lists and new.operator is Operator.NOT_IN is existing.operator:
        # A wider deny-list is tighter.
        return set(new.value) > set(existing.value)
    return None


def is_unsatisfiable(a: CompiledRule, b: CompiledRule) -> bool:
    """Would these two together forbid every purchase?"""
    if not _comparable(a, b):
        return False
    lhs, rhs = _as_decimal(a.value), _as_decimal(b.value)
    if lhs is not None and rhs is not None:
        if a.operator in _UPPER_BOUND and b.operator in _LOWER_BOUND:
            return lhs < rhs
        if a.operator in _LOWER_BOUND and b.operator in _UPPER_BOUND:
            return lhs > rhs
    both_lists = isinstance(a.value, list) and isinstance(b.value, list)
    if both_lists and a.operator is Operator.IN is b.operator:
        # Two allow-lists with nothing in common leave nothing allowed.
        return not (set(a.value) & set(b.value))
    return False


def review_amendment(
    existing: list[CompiledRule],
    proposed: list[CompiledRule],
    *,
    current_policy: UncertaintyPolicy,
    new_policy: UncertaintyPolicy | None = None,
) -> AmendmentReview:
    """Check a proposed change before it reaches the platform.

    `proposed` is the full intended rule set, not a delta.
    """
    review = AmendmentReview()
    kept = {r.identity() for r in proposed}

    for rule in existing:
        if rule.identity() not in kept:
            review.allowed = False
            review.problems.append(
                f"'{rule.field} {rule.operator.value} {rule.value}' would be removed. "
                "A live policy can only be tightened; revoke it instead if you want "
                "to start over."
            )

    known = {r.identity() for r in existing}
    review.added = [r for r in proposed if r.identity() not in known]

    for rule in review.added:
        for old in existing:
            if is_unsatisfiable(rule, old):
                review.allowed = False
                review.problems.append(
                    f"'{rule.field} {rule.operator.value} {rule.value}' cannot hold at "
                    f"the same time as '{old.field} {old.operator.value} {old.value}'. "
                    "Together they would block every purchase."
                )
                continue
            tighter = binds_more_tightly(rule, old)
            if tighter is False:
                # Harmless under conjunction, but not what the customer expects.
                review.notes.append(
                    f"'{rule.field} {rule.operator.value} {rule.value}' is looser than "
                    f"the existing '{old.field} {old.operator.value} {old.value}', so "
                    "it changes nothing — the existing limit still governs. Revoke and "
                    "re-authorise if you meant to relax it."
                )
            elif tighter is True:
                review.notes.append(
                    f"'{rule.field} {rule.operator.value} {rule.value}' now governs, "
                    f"replacing '{old.field} {old.operator.value} {old.value}' in "
                    "practice."
                )

    if new_policy is not None and (current_policy, new_policy) not in ALLOWED_POLICY_MOVES:
        review.allowed = False
        review.problems.append(
            f"Handling of uncertainty cannot move from '{current_policy.value}' to "
            f"'{new_policy.value}' on a live policy. It can only move toward "
            "'decline'; anything else needs a new policy."
        )
    return review
