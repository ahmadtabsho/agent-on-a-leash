"""The engine, run against every attempt the pack supplies.

Nothing here asserts a scenario's "expected answer" — the pack has no answer
key and the brief forbids deciding from a scenario's name or position. What is
asserted is the reasoning: that a breach of a stated limit declines, that an
unestablished fact reaches the customer rather than being guessed, and that no
merchant text can make the engine more permissive than the policy allows.
"""

from decimal import Decimal

import pytest

from leash.decision import DecisionEngine, RunState
from leash.decision.evidence import Outcome
from leash.models.enums import Decision, UncertaintyPolicy
from leash.policy import compile_policy
from leash.replay.builder import EventBuilder


@pytest.fixture(scope="module")
def builder() -> EventBuilder:
    return EventBuilder()


@pytest.fixture(scope="module")
def engine() -> DecisionEngine:
    return DecisionEngine()


def run(builder, engine, scenario_id, **overrides):
    """Replay one scenario end to end, returning {source_id: verdict}."""
    instruction = builder.catalogue[scenario_id]["cardholder_instruction"]
    policy = compile_policy(instruction)
    for key, value in overrides.items():
        setattr(policy, key, value)
    replay = builder.build(scenario_id, policy)
    state = RunState(run_id=scenario_id)
    return {
        event.authorization.source_authorization_id: engine.decide(event, state)
        for event in replay.events
    }, state


def codes(verdict) -> set[str]:
    return {f.code for f in verdict.findings if f.outcome is not Outcome.PASS}


# --- the whole pack --------------------------------------------------------


def test_every_attempt_produces_a_decision_with_reasons(builder, engine):
    total = 0
    for scenario_id in builder.scenario_ids():
        verdicts, _ = run(builder, engine, scenario_id)
        for source_id, verdict in verdicts.items():
            total += 1
            assert verdict.decision in set(Decision), source_id
            assert verdict.customer_message, source_id
            assert verdict.findings, source_id
            if verdict.decision is not Decision.APPROVE:
                assert verdict.reason_codes, source_id
    assert total == 45


def test_every_decision_lands_far_inside_the_deadline(builder, engine):
    """The platform allows eight seconds. A pure engine should not need one."""
    for scenario_id in builder.scenario_ids():
        verdicts, _ = run(builder, engine, scenario_id)
        for source_id, verdict in verdicts.items():
            assert verdict.elapsed_ms < 100, (source_id, verdict.elapsed_ms)


def test_each_scenario_exercises_more_than_one_outcome(builder, engine):
    """A layer that approves everything, or blocks everything, is not a control
    layer. Only the single-event connection check is allowed to be uniform."""
    for scenario_id in builder.scenario_ids():
        verdicts, _ = run(builder, engine, scenario_id)
        outcomes = {v.decision for v in verdicts.values()}
        if len(verdicts) > 1:
            assert len(outcomes) > 1, scenario_id


# --- a stated limit is enforced on the purchase that breaches it -----------


def test_a_per_order_limit_declines_the_order_that_exceeds_it(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0001")
    assert verdicts["AU0003"].decision is Decision.APPROVE     # exactly CHF 120
    assert verdicts["AU0004"].decision is Decision.DECLINE     # CHF 126
    assert "rule_breached" in codes(verdicts["AU0004"])


def test_a_period_limit_counts_the_order_being_decided(builder, engine):
    """"Keep the total across any seven days at or below CHF 300" is a claim
    about the total including this order. Comparing only prior spend would let
    the purchase that actually breaks the limit through."""
    verdicts, _ = run(builder, engine, "SCEN0001")
    approved_before_breach = Decimal("44.50") + Decimal("120.00") + Decimal("70.00") + Decimal("65.00")
    assert approved_before_breach == Decimal("299.50")
    assert verdicts["AU0007"].decision is Decision.DECLINE
    assert any("would take the 7-day total" in f.detail for f in verdicts["AU0007"].findings)


def test_a_rolling_window_releases_spend_as_it_slides(builder, engine):
    """The last order is approved because earlier spend has aged out, not
    because the limit was forgotten."""
    verdicts, _ = run(builder, engine, "SCEN0001")
    assert verdicts["AU0011"].decision is Decision.APPROVE


def test_a_paused_purchase_is_not_yet_spend(builder, engine):
    replay = builder.build("SCEN0001", compile_policy(builder.catalogue["SCEN0001"]["cardholder_instruction"]))
    event = replay.events[1]
    state = RunState()
    state.record(event.authorization, Decision.STEP_UP)
    when = event.authorization.timestamp

    assert state.approved_spend_within(7, when) == Decimal(0)
    assert state.pending_spend_within(7, when) == Decimal("120.00")

    state.resolve(event.authorization.authorization_id, Decision.APPROVE)
    assert state.approved_spend_within(7, when) == Decimal("120.00")
    assert state.pending_spend_within(7, when) == Decimal(0)


def test_delivery_fee_is_not_added_twice(builder, engine):
    """AU0003 is CHF 114 of goods plus CHF 6 delivery, exactly at the limit.
    Adding the fee again would push it over and decline it."""
    verdicts, _ = run(builder, engine, "SCEN0001")
    assert verdicts["AU0003"].decision is Decision.APPROVE


# --- what is in the basket, not what the shop is --------------------------


def test_an_off_purpose_line_breaches_a_basket_rule(builder, engine):
    """A supermarket sells cosmetics without ceasing to be a grocery merchant,
    so the rule has to look at the lines."""
    verdicts, _ = run(builder, engine, "SCEN0001")
    verdict = verdicts["AU0007"]
    assert verdict.decision is Decision.DECLINE
    assert any("Fragrance" in f.detail for f in verdict.findings)


def test_a_retailer_type_rule_rejects_the_wrong_kind_of_shop(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0002")
    assert verdicts["AU0022"].decision is Decision.DECLINE   # sustainable_goods


def test_an_unfamiliar_but_fully_compliant_seller_is_allowed(builder, engine):
    """This instruction says nothing about familiarity, so a new shop that
    meets every stated condition must not be blocked for being new."""
    verdicts, _ = run(builder, engine, "SCEN0002")
    assert verdicts["AU0023"].decision is Decision.APPROVE


# --- what was actually asked for ------------------------------------------


def test_a_substituted_size_is_caught(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0002")
    assert verdicts["AU0013"].decision is Decision.DECLINE
    assert any("size 42" in f.detail for f in verdicts["AU0013"].findings)


def test_a_different_kind_of_shoe_is_not_the_one_requested(builder, engine):
    """The trail shoe's copy mentions a "lugged off-road sole". Identity comes
    from the item, not from words that happen to appear in the seller's prose."""
    verdicts, _ = run(builder, engine, "SCEN0002")
    verdict = verdicts["AU0017"]
    assert verdict.decision is Decision.DECLINE
    assert any("Trail-running shoes" in f.detail for f in verdict.findings)


def test_a_different_product_entirely_is_caught(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0002")
    assert verdicts["AU0020"].decision is Decision.DECLINE   # a cycling helmet
    verdicts, _ = run(builder, engine, "SCEN0004")
    assert verdicts["AU0043"].decision is Decision.DECLINE   # a gift voucher


def test_an_explicit_no_extras_instruction_declines_an_add_on(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0004")
    assert verdicts["AU0041"].decision is Decision.DECLINE


def test_an_add_on_is_raised_even_without_an_explicit_instruction(builder, engine):
    """SCEN0002's customer never said "no extras", but being sold a protection
    plan alongside the shoes is still worth their attention."""
    verdicts, _ = run(builder, engine, "SCEN0002")
    verdict = verdicts["AU0018"]
    assert verdict.decision is Decision.STEP_UP
    assert verdict.fell_back_to_policy is UncertaintyPolicy.ASK


# --- order terms: unknown is neither yes nor no ---------------------------


def test_a_stated_window_that_is_too_short_is_a_breach(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0002")
    assert verdicts["AU0015"].decision is Decision.DECLINE   # 7 days, needs 14
    assert verdicts["AU0019"].decision is Decision.APPROVE   # exactly 14


def test_a_final_sale_is_a_breach(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0002")
    assert verdicts["AU0014"].decision is Decision.DECLINE


def test_an_unstated_return_policy_reaches_the_customer(builder, engine):
    """The seller did not say. That is not permission, and it is not a refusal
    either — it is exactly the case the customer said to ask about."""
    verdicts, _ = run(builder, engine, "SCEN0002")
    verdict = verdicts["AU0016"]
    assert verdict.decision is Decision.STEP_UP
    assert "fact_not_established" in codes(verdict)


# --- session signals ------------------------------------------------------


def test_a_new_device_pauses_rather_than_refuses(builder, engine):
    """The customer said "pause". An unfamiliar device is a new laptop as often
    as it is an intruder, so it is never a definite breach."""
    verdicts, _ = run(builder, engine, "SCEN0003")
    verdict = verdicts["AU0026"]
    assert verdict.decision is Decision.STEP_UP
    assert "signal_breached" in codes(verdict)


def test_ordinary_shopping_resumes_once_the_session_recovers(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0003")
    assert verdicts["AU0031"].decision is Decision.APPROVE
    assert verdicts["AU0032"].decision is Decision.APPROVE


def test_a_foreign_purchase_on_a_familiar_device_is_not_suspicious(builder, engine):
    """Converted at the fixed rate, AU0032 is CHF 247.00 — inside the limit."""
    verdicts, _ = run(builder, engine, "SCEN0003")
    assert verdicts["AU0032"].decision is Decision.APPROVE


# --- manipulation ---------------------------------------------------------


def test_a_purchase_is_judged_on_its_facts_not_on_the_text_around_it(builder, engine):
    """AU0037's copy claims a CHF 900 pre-authorisation. The order is CHF 520
    against a CHF 400 limit, and that is the only thing that decides it."""
    verdicts, _ = run(builder, engine, "SCEN0004")
    verdict = verdicts["AU0037"]
    assert verdict.decision is Decision.DECLINE
    assert any(f.code == "rule_breached" and f.blocks for f in verdict.findings)


def test_an_injection_never_produces_an_approval(builder, engine):
    """AU0040 is inside every stated limit and would otherwise approve. The
    seller's attempt to instruct the decider is itself the intervention."""
    verdicts, _ = run(builder, engine, "SCEN0004")
    verdict = verdicts["AU0040"]
    assert verdict.decision is Decision.STEP_UP
    assert "merchant_text_targets_decider" in codes(verdict)


def test_a_lookalike_seller_is_recognised_by_identifier(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0004")
    verdict = verdicts["AU0039"]
    assert verdict.decision is Decision.DECLINE
    assert any("PixelHarbor" in (f.expected or "") for f in verdict.findings)


def test_a_repeated_basket_under_a_new_id_is_raised(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0004")
    verdict = verdicts["AU0036"]
    assert verdict.decision is Decision.STEP_UP
    assert "possible_duplicate_order" in codes(verdict)


def test_a_legitimate_re_quote_after_a_decline_is_allowed(builder, engine):
    """AU0042 follows the declined AU0037 at a lower price. A different amount
    is a new offer, not a duplicate."""
    verdicts, _ = run(builder, engine, "SCEN0004")
    assert verdicts["AU0042"].decision is Decision.APPROVE


# --- nothing keys off identity, name, or position -------------------------


def test_decisions_do_not_depend_on_scenario_id_or_position(builder, engine):
    """The brief forbids deciding from a scenario name, request ID or sequence
    position. Rewriting all three must change nothing."""
    scenario_id = "SCEN0002"
    policy = compile_policy(builder.catalogue[scenario_id]["cardholder_instruction"])

    original = builder.build(scenario_id, policy)
    state = RunState()
    baseline = [engine.decide(e, state).decision for e in original.events]

    disguised = builder.build(scenario_id, policy)
    state = RunState()
    rewritten = []
    for offset, event in enumerate(disguised.events):
        payload = event.model_dump(mode="json")
        payload["authorization"]["scenario_id"] = "SCEN9999"
        payload["authorization"]["replay_order"] = len(disguised.events) - offset
        payload["request_id"] = f"req_disguised_{offset}"
        from leash.models import parse_event

        rewritten.append(engine.decide(parse_event(payload), state).decision)

    assert rewritten == baseline


# --- repeated delivery ----------------------------------------------------


def test_a_repeated_delivery_replays_the_saved_answer(builder, engine):
    replay = builder.build("SCEN0001", compile_policy(builder.catalogue["SCEN0001"]["cardholder_instruction"]))
    state = RunState()
    first = engine.decide(replay.events[0], state)
    second = engine.decide(replay.events[0], state)

    assert second.decision is first.decision
    assert second.reason_codes == ["idempotent_replay"]
    amount = replay.events[0].authorization.billing_amount_chf
    assert state.approved_spend_within(7, replay.events[0].authorization.timestamp) == amount


# --- the customer's own fallback governs uncertainty ----------------------


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (UncertaintyPolicy.ASK, Decision.STEP_UP),
        (UncertaintyPolicy.DECLINE, Decision.DECLINE),
        (UncertaintyPolicy.APPROVE, Decision.APPROVE),
    ],
)
def test_uncertainty_is_settled_by_the_customers_choice(builder, engine, policy, expected):
    """AU0016's return policy is unstated. What happens next is the customer's
    call, not ours."""
    verdicts, _ = run(builder, engine, "SCEN0002", uncertainty_policy=policy)
    assert verdicts["AU0016"].decision is expected


def test_a_definite_breach_is_not_softened_by_an_approve_fallback(builder, engine):
    """Choosing "approve when unsure" must not approve a stated breach."""
    verdicts, _ = run(builder, engine, "SCEN0002", uncertainty_policy=UncertaintyPolicy.APPROVE)
    assert verdicts["AU0021"].decision is Decision.DECLINE   # CHF 215 over CHF 200


# --- the decision payload -------------------------------------------------


def test_decision_payload_matches_the_documented_body(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0004")
    verdict = verdicts["AU0040"]
    payload = verdict.to_decision_payload("AUTH_LIVE_1")

    assert payload["authorization_id"] == "AUTH_LIVE_1"
    assert payload["decision"] == "step_up"
    assert payload["reason_codes"][0] == "customer_confirmation"
    assert payload["customer_message"]
    assert payload["evidence"] and all("code" in e for e in payload["evidence"])
    assert payload["engine_version"].startswith("leash/")


def test_an_approval_carries_no_adverse_evidence(builder, engine):
    verdicts, _ = run(builder, engine, "SCEN0000")
    payload = verdicts["AU0001"].to_decision_payload("AUTH_LIVE_1")
    assert payload["decision"] == "approve"
    assert "evidence" not in payload


# --- preconditions --------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("mandate", "status"), "revoked", "mandate_not_active"),
        (("authorization", "authority_status"), "revoked", "authority_not_active"),
        (("authorization", "card_status_at_attempt"), "blocked", "card_not_active"),
    ],
)
def test_a_withdrawn_permission_stops_the_purchase(builder, engine, path, value, code):
    from leash.models import parse_event

    replay = builder.build("SCEN0000", compile_policy(builder.catalogue["SCEN0000"]["cardholder_instruction"]))
    payload = replay.events[0].model_dump(mode="json")
    payload[path[0]][path[1]] = value

    verdict = engine.decide(parse_event(payload), RunState())
    assert verdict.decision is Decision.DECLINE
    assert code in codes(verdict)
