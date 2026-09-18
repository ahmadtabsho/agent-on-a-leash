"""The three things the brief asks a demo to show.

1. An ordinary purchase completing with little friction.
2. An ambiguous, unsafe or manipulated purchase getting a useful intervention.
3. The customer's own approval, rejection, or revocation path.

Nothing here is staged. Each moment is selected from a live replay by what the
engine actually decided, so if the engine changes its mind the demo changes
with it rather than narrating something that no longer happens.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..decision import DecisionEngine, RunState
from ..decision.evidence import Outcome
from ..models.enums import Decision, UncertaintyPolicy
from ..policy import compile_policy, review_amendment
from .builder import EventBuilder


@dataclass
class Moment:
    heading: str
    scenario_id: str
    source_id: str
    merchant: str
    amount_chf: float
    decision: str
    message: str
    evidence: list[tuple[str, str, str]]
    elapsed_ms: float


def _evidence(verdict) -> list[tuple[str, str, str]]:
    return [
        (f.outcome.value, f.code, f.detail)
        for f in verdict.findings
        if f.outcome is not Outcome.PASS
    ]


def collect(scenario_id: str, builder: EventBuilder, engine: DecisionEngine):
    policy = compile_policy(builder.catalogue[scenario_id]["cardholder_instruction"])
    replay = builder.build(scenario_id, policy)
    state = RunState(run_id=scenario_id)
    return policy, state, [(e, engine.decide(e, state)) for e in replay.events]


def _moment(heading, scenario_id, pair) -> Moment:
    event, verdict = pair
    auth = event.authorization
    return Moment(
        heading=heading,
        scenario_id=scenario_id,
        source_id=auth.source_authorization_id,
        merchant=auth.merchant.merchant_name,
        amount_chf=float(auth.billing_amount_chf),
        decision=verdict.decision.value,
        message=verdict.customer_message,
        evidence=_evidence(verdict),
        elapsed_ms=verdict.elapsed_ms,
    )


def build_demo() -> dict:
    builder, engine = EventBuilder(), DecisionEngine()
    out: dict = {"moments": [], "policy_control": [], "slowest_ms": 0.0}

    ordinary = None
    intervention = None
    human = None

    for scenario_id in builder.scenario_ids():
        _, _, pairs = collect(scenario_id, builder, engine)
        for pair in pairs:
            out["slowest_ms"] = max(out["slowest_ms"], pair[1].elapsed_ms)
            decision = pair[1].decision
            if ordinary is None and decision is Decision.APPROVE and not _evidence(pair[1]):
                ordinary = _moment(
                    "An ordinary purchase, with no friction", scenario_id, pair
                )
            if intervention is None and scenario_id == "SCEN0004":
                codes = {c for _, c, _ in _evidence(pair[1])}
                if "merchant_text_targets_decider" in codes and decision is Decision.STEP_UP:
                    intervention = _moment(
                        "A seller tries to instruct the decider", scenario_id, pair
                    )
            if human is None and decision is Decision.STEP_UP and scenario_id == "SCEN0002":
                human = _moment("A purchase we will not settle alone", scenario_id, pair)

    out["moments"] = [m for m in (ordinary, intervention, human) if m]

    # The customer's own controls, exercised rather than described.
    instruction = builder.catalogue["SCEN0001"]["cardholder_instruction"]
    policy = compile_policy(instruction)
    tighten = review_amendment(
        policy.hard_rules,
        policy.hard_rules,
        current_policy=UncertaintyPolicy.ASK,
        new_policy=UncertaintyPolicy.DECLINE,
    )
    loosen = review_amendment(
        policy.hard_rules,
        policy.hard_rules[1:],
        current_policy=UncertaintyPolicy.ASK,
    )
    out["policy_control"] = [
        ("Tighten to refuse when unsure", tighten.allowed, ""),
        (
            "Remove a confirmed rule",
            loosen.allowed,
            loosen.problems[0] if loosen.problems else "",
        ),
    ]
    out["open_questions"] = compile_policy(
        builder.catalogue["SCEN0002"]["cardholder_instruction"]
    ).open_questions
    return out
