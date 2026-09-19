"""The decision.

A pure function of `(event, run state)`. No network, no clock beyond what it is
handed, no I/O. That is what makes it fast enough for the deadline, testable
offline against every supplied attempt, and impossible for a merchant to talk
out of an answer.

Stages run in order and each appends to a shared ledger. The verdict is derived
from the ledger at the end rather than returned early from the middle, so the
explanation always accounts for everything the engine looked at.

The resolution rule, in one line: a definite breach declines, anything left
unsettled falls to the customer's own `uncertainty_policy`, and only a clean
pass approves.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..llm.advisor import Advice, IntentAdvisor
from ..llm.clarifier import ClarificationPlanner
from ..models.enums import (
    AuthorityStatus,
    CardStatus,
    Decision,
    MandateStatus,
    UncertaintyPolicy,
)
from ..models.events import AuthorizationEvent
from .evidence import Ledger, Outcome, Stage, Verdict
from .facts import DEFAULT_FAMILIARITY_THRESHOLD, Facts
from .history import CardHistory, default_history
from .rules import evaluate_rule
from .state import RunState

ENGINE_VERSION = "leash/0.1.0"


@dataclass(frozen=True)
class EngineConfig:
    familiarity_threshold: int = DEFAULT_FAMILIARITY_THRESHOLD
    engine_version: str = ENGINE_VERSION
    # An order repeating an earlier basket within this many minutes is treated
    # as a possible duplicate rather than a second deliberate purchase.
    duplicate_window_minutes: int = 180


class DecisionEngine:
    """Decides whether one proposed purchase may go ahead."""

    def __init__(
        self,
        history: CardHistory | None = None,
        config: EngineConfig | None = None,
        advisor: IntentAdvisor | None = None,
        clarifier: ClarificationPlanner | None = None,
    ):
        self.history = history if history is not None else default_history()
        self.config = config or EngineConfig()
        # Optional. Everything below decides identically when it is unavailable.
        self.advisor = advisor if advisor is not None else IntentAdvisor()
        self.clarifier = clarifier or ClarificationPlanner()

    # --- stages ------------------------------------------------------------

    def _preconditions(self, event: AuthorizationEvent, ledger: Ledger) -> None:
        """Facts about permission itself, before any judgement is needed."""
        auth = event.authorization
        mandate = event.mandate

        if mandate.status is not MandateStatus.ACTIVE:
            ledger.add(
                Stage.PRECONDITIONS,
                "mandate_not_active",
                Outcome.FAIL,
                f"the permission this purchase relies on is {mandate.status.value}",
                field="mandate.status",
                observed=mandate.status.value,
                expected=MandateStatus.ACTIVE.value,
            )
        if auth.authority_status is not AuthorityStatus.ACTIVE:
            ledger.add(
                Stage.PRECONDITIONS,
                "authority_not_active",
                Outcome.FAIL,
                f"the authority for this card is {auth.authority_status.value}",
                field="authorization.authority_status",
                observed=auth.authority_status.value,
            )
        if auth.card_status_at_attempt is not CardStatus.ACTIVE:
            ledger.add(
                Stage.PRECONDITIONS,
                "card_not_active",
                Outcome.FAIL,
                f"the card was {auth.card_status_at_attempt.value} at the time of this purchase",
                field="authorization.card_status_at_attempt",
                observed=auth.card_status_at_attempt.value,
            )
        if mandate.card_id != auth.card_id:
            ledger.add(
                Stage.PRECONDITIONS,
                "card_mismatch",
                Outcome.FAIL,
                "this purchase is on a different card from the one you authorised",
                field="authorization.card_id",
                observed=auth.card_id,
                expected=mandate.card_id,
            )

    def _sanitise(self, facts: Facts, ledger: Ledger) -> None:
        """Record any attempt by the shop to steer this decision.

        Nothing here can change a rule — the sanitiser returns data and has no
        access to the policy. What it does change is how much weight the rest
        of the seller's copy can carry.
        """
        for item, text in facts.manipulated_lines:
            codes = ", ".join(finding.code for finding in text.injections)
            ledger.add(
                Stage.SANITISE,
                "merchant_text_targets_decider",
                Outcome.UNCERTAIN,
                f'the product text for line {item.line_no} ("{item.item_name}") is written '
                f"to instruct an automated purchaser rather than to describe the product "
                f"[{codes}]: {text.injections[0].excerpt}",
                field="authorization.items.item_details",
            )
        if facts.order_text.is_manipulated:
            ledger.add(
                Stage.SANITISE,
                "order_text_targets_decider",
                Outcome.UNCERTAIN,
                "the order description is written to instruct an automated purchaser: "
                f"{facts.order_text.injections[0].excerpt}",
                field="authorization.purchase_description",
            )

    def _hard_rules(self, event: AuthorizationEvent, facts: Facts, ledger: Ledger) -> None:
        if not event.mandate.hard_rules:
            ledger.add(
                Stage.HARD_RULES,
                "no_rules_confirmed",
                Outcome.UNCERTAIN,
                "your policy contains no executable checks, so nothing about this "
                "purchase can be confirmed against it",
            )
            return
        for rule in event.mandate.hard_rules:
            ledger.findings.append(evaluate_rule(rule, facts))

    def _second_opinion(self, event: AuthorizationEvent, facts: Facts, ledger: Ledger) -> None:
        """Ask the optional advisor whether an apparent match really matches.

        Strictly one-directional. The advisor can turn an apparent match into
        uncertainty; it can never turn uncertainty into a match, and it never
        sees the policy. The worst a wrong or compromised model can do is send
        a purchase to the customer — it cannot approve one.
        """
        if self.advisor is None or not self.advisor.available:
            return
        requested = facts._requested_phrase()
        if not requested:
            return
        matched = facts.matching_lines()
        if not matched or len(matched) == len(event.authorization.items) == 0:
            return

        # One budget for the whole event, not one per line. A three-line basket
        # asking three times could spend three timeouts and eat the deadline.
        spent_ms = 0.0
        budget_ms = float(self.advisor.settings.llm_timeout_ms)

        for item in matched:
            if spent_ms >= budget_ms:
                return
            result = self.advisor.compare(requested, item.item_name, item.item_category)
            if result is None:
                # Unavailable, slow, or unusable. The deterministic answer stands.
                return
            spent_ms += result.elapsed_ms
            if result.advice is Advice.MATCH:
                continue
            ledger.add(
                Stage.SIGNALS,
                "intent_match_disputed",
                Outcome.UNCERTAIN,
                f'a second check was not satisfied that line {item.line_no}, '
                f'"{item.item_name}", is the "{requested}" you asked for '
                f"({result.why or result.advice.value})",
                field="derived.requested_item_match",
                observed=item.item_name,
                expected=requested,
            )

    def _signals(self, event: AuthorizationEvent, facts: Facts, state: RunState, ledger: Ledger) -> None:
        """Observations no rule asked for, but a customer would want raised."""
        auth = event.authorization

        impostor = facts.lookalike
        if impostor:
            ledger.add(
                Stage.SIGNALS,
                "lookalike_merchant",
                Outcome.UNCERTAIN,
                f'"{auth.merchant.merchant_name}" is a shop this card has never used, and '
                f'its name closely resembles "{impostor.familiar_name}", which you do use '
                f"({impostor.similarity:.0%} similar)",
                field="authorization.merchant.merchant_name",
                observed=auth.merchant.merchant_name,
                expected=impostor.familiar_name,
            )

        duplicates = state.matching_baskets(
            auth, within=_minutes(self.config.duplicate_window_minutes)
        )
        if duplicates:
            earlier = duplicates[0]
            ledger.add(
                Stage.SIGNALS,
                "possible_duplicate_order",
                Outcome.UNCERTAIN,
                f"this is the same basket, from the same shop, for the same amount as an "
                f"order {_ago(earlier.timestamp, auth.timestamp)} earlier, which was "
                f"{earlier.decision.value}d",
                field="authorization.items",
                observed=auth.authorization_id,
                expected=earlier.authorization_id,
            )

        # Extras the customer did not ask for. When they explicitly forbade
        # additions, a hard rule already covers it and declines; when they did
        # not, raising it is still the right thing to do.
        extras = facts.unrequested_lines()
        forbidden = any(
            rule.field == "derived.unrequested_line_count" for rule in event.mandate.hard_rules
        )
        if extras and not forbidden:
            names = ", ".join(f'"{i.item_name}"' for i in extras)
            ledger.add(
                Stage.SIGNALS,
                "unrequested_addition",
                Outcome.UNCERTAIN,
                f"the order also contains {names}, which is not part of what you asked for",
                field="authorization.items",
                observed=names,
            )

        for item in auth.items:
            text = facts.line_text[item.line_no]
            if text.trustworthy_for_facts and text.facts.recurring_charge:
                ledger.add(
                    Stage.SIGNALS,
                    "recurring_commitment",
                    Outcome.UNCERTAIN,
                    f'line {item.line_no}, "{item.item_name}", commits you to a repeating '
                    "charge rather than a one-off purchase",
                    field="authorization.items.item_details",
                )

        if not auth.totals_reconcile:
            ledger.add(
                Stage.SIGNALS,
                "totals_do_not_reconcile",
                Outcome.UNCERTAIN,
                f"the shop's figures do not add up: {auth.items_subtotal} plus delivery "
                f"{auth.delivery_fee} is {auth.stated_total}, but the order total is "
                f"{auth.amount}",
                field="authorization.amount",
                observed=str(auth.amount),
                expected=str(auth.stated_total),
            )
        if not auth.billing_amount_reconciles:
            ledger.add(
                Stage.SIGNALS,
                "billing_amount_does_not_reconcile",
                Outcome.UNCERTAIN,
                f"converting {auth.amount} {auth.currency.value} at the fixed rate does not "
                f"give the stated CHF total of {auth.billing_amount_chf}",
                field="authorization.billing_amount_chf",
            )

    # --- resolution --------------------------------------------------------

    def _resolve(self, event: AuthorizationEvent, ledger: Ledger, started: float) -> Verdict:
        policy = event.mandate.uncertainty_policy
        blocking = [f for f in ledger.findings if f.blocks]
        unsettled = [f for f in ledger.findings if f.unsettles]

        if blocking:
            decision, fallback = Decision.DECLINE, None
            message = _sentence(f.detail for f in blocking[:2])
        elif unsettled:
            decision, fallback = policy.to_decision(), policy
            message = _sentence(f.detail for f in unsettled[:2])
            if policy is UncertaintyPolicy.APPROVE:
                message = f"Approved as you asked us to when unsure. {message}"
        else:
            decision, fallback = Decision.APPROVE, None
            message = "This purchase matches everything you authorised."

        reason_codes = list(dict.fromkeys(f.code for f in (blocking or unsettled)))
        if decision is Decision.STEP_UP:
            reason_codes.insert(0, "customer_confirmation")

        clarification = None
        if decision is Decision.STEP_UP and unsettled:
            clarification = self.clarifier.plan(unsettled[0])

        return Verdict(
            decision=decision,
            findings=ledger.findings,
            reason_codes=reason_codes,
            customer_message=message,
            engine_version=self.config.engine_version,
            fell_back_to_policy=fallback,
            clarification=clarification,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    # --- entry point -------------------------------------------------------

    def decide(self, event: AuthorizationEvent, state: RunState) -> Verdict:
        """Decide one purchase.

        A purchase already answered in this run is answered the same way again
        rather than re-evaluated. Repeated delivery must not move the spend
        total or produce a different answer the second time.
        """
        started = time.perf_counter()
        auth = event.authorization

        previous = state.answered(auth.authorization_id)
        if previous is not None:
            ledger = Ledger()
            ledger.add(
                Stage.RESOLVE,
                "already_answered",
                Outcome.PASS,
                f"this purchase was already answered in this run: {previous.decision.value}",
                observed=previous.decision.value,
            )
            return Verdict(
                decision=previous.decision,
                findings=ledger.findings,
                reason_codes=["idempotent_replay"],
                customer_message="Already answered; the original answer stands.",
                engine_version=self.config.engine_version,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

        ledger = Ledger()
        self._preconditions(event, ledger)

        facts = Facts(
            event=event,
            state=state,
            history=self.history,
            familiarity_threshold=self.config.familiarity_threshold,
        )
        self._sanitise(facts, ledger)
        self._hard_rules(event, facts, ledger)
        self._second_opinion(event, facts, ledger)
        self._signals(event, facts, state, ledger)

        verdict = self._resolve(event, ledger, started)
        state.record(auth, verdict.decision)
        return verdict


def _minutes(count: int):
    from datetime import timedelta

    return timedelta(minutes=count)


def _ago(earlier, later) -> str:
    delta = later - earlier
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minute(s)"
    hours = minutes // 60
    return f"{hours} hour(s)" if hours < 48 else f"{hours // 24} day(s)"


def _sentence(details) -> str:
    parts = [d.rstrip(".") for d in details]
    if not parts:
        return ""
    body = parts[0] if len(parts) == 1 else f"{parts[0]}; also, {parts[1]}"
    return body[0].upper() + body[1:] + "."
