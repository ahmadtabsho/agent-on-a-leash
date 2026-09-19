"""The compiler is graded on two things: the checks it produces, and the
questions it refuses to answer on the customer's behalf.

Tests run against the five real cardholder instructions in the pack, read from
the catalogue rather than pasted in, so a change to the fixtures surfaces here.
Nothing keys off a scenario ID — the instruction text is the only input.
"""

import pytest

from leash.datapack import load_table
from leash.models.enums import Currency, Operator, RuleScope, UncertaintyPolicy
from leash.policy import compile_policy, review_amendment
from leash.policy.amend import binds_more_tightly, is_unsatisfiable
from leash.policy.compiler import CompiledRule
from leash.policy.vocabulary import FIELDS


@pytest.fixture(scope="module")
def instructions() -> dict[str, str]:
    return {r["scenario_id"]: r["cardholder_instruction"] for r in load_table("scenario_catalogue")}


def rule_for(policy, field: str) -> CompiledRule | None:
    return next((r for r in policy.hard_rules if r.field == field), None)


def rules_for(policy, field: str) -> list[CompiledRule]:
    return [r for r in policy.hard_rules if r.field == field]


# --- every instruction compiles to something usable ------------------------


def test_every_instruction_produces_rules_and_a_confirmable_summary(instructions):
    for scenario, text in instructions.items():
        policy = compile_policy(text)
        assert policy.hard_rules, scenario
        assert policy.guidance, scenario
        assert policy.instruction == text, "the original wording must go out unchanged"


def test_every_compiled_field_is_one_the_engine_resolves(instructions):
    """A field nothing resolves is a rule that silently never fires."""
    for text in instructions.values():
        for rule in compile_policy(text).hard_rules:
            assert rule.field in FIELDS, rule.field


def test_every_rule_payload_matches_the_stored_rule_format(instructions):
    allowed_keys = {"field", "operator", "value", "currency", "scope", "period_days"}
    for text in instructions.values():
        for rule in compile_policy(text).hard_rules:
            payload = rule.to_payload()
            assert set(payload) <= allowed_keys
            assert payload["operator"] in {o.value for o in Operator}
            value = payload["value"]
            assert not isinstance(value, bool), "a rule value is never a boolean"
            assert isinstance(value, (int, float, str, list))
            if isinstance(value, list):
                assert all(isinstance(v, str) for v in value), "lists hold only strings"


def test_optional_fields_are_omitted_rather_than_sent_as_null(instructions):
    for text in instructions.values():
        for rule in compile_policy(text).hard_rules:
            assert None not in rule.to_payload().values()


# --- the specific things each instruction says -----------------------------


def test_per_order_and_rolling_period_limits_are_separated(instructions):
    policy = compile_policy(instructions["SCEN0001"])

    per_order = rule_for(policy, "authorization.billing_amount_chf")
    assert (per_order.operator, per_order.value) == (Operator.LTE, 120.0)
    assert per_order.scope is RuleScope.PURCHASE
    assert per_order.period_days is None

    period = rule_for(policy, "derived.spend_in_period_chf")
    assert (period.operator, period.value) == (Operator.LTE, 300.0)
    assert period.scope is RuleScope.PERIOD
    assert period.period_days == 7, "'any seven days' is a rolling seven-day window"
    assert period.currency is Currency.CHF


def test_a_purchase_purpose_constrains_the_basket_not_the_shop(instructions):
    """A supermarket sells cosmetics without ceasing to be a grocery merchant."""
    policy = compile_policy(instructions["SCEN0001"])
    basket = rule_for(policy, "item.item_category")
    assert basket is not None and basket.operator is Operator.IN
    assert "groceries" in basket.value
    assert rule_for(policy, "authorization.merchant.merchant_category") is None


def test_a_named_retailer_type_constrains_the_shop(instructions):
    policy = compile_policy(instructions["SCEN0002"])
    shop = rule_for(policy, "authorization.merchant.merchant_category")
    assert shop is not None and shop.value == ["sporting_goods"]


def test_return_window_requires_both_a_length_and_a_stated_policy(instructions):
    policy = compile_policy(instructions["SCEN0002"])
    window = rule_for(policy, "derived.return_window_days")
    assert (window.operator, window.value) == (Operator.GTE, 14)
    stated = rule_for(policy, "authorization.order_returnable")
    assert (stated.operator, stated.value) == (Operator.EQ, "true")


def test_requested_item_and_its_attributes_are_captured(instructions):
    shoes = compile_policy(instructions["SCEN0002"])
    assert rule_for(shoes, "derived.requested_item").value == "road-running shoes"
    assert any(r.value == "size=43" for r in rules_for(shoes, "derived.requested_attribute"))

    monitor = compile_policy(instructions["SCEN0004"])
    assert rule_for(monitor, "derived.requested_item").value == "27-inch monitor"
    assert any(r.value == "inch=27" for r in rules_for(monitor, "derived.requested_attribute"))


def test_no_extras_becomes_an_executable_check(instructions):
    policy = compile_policy(instructions["SCEN0004"])
    extras = rule_for(policy, "derived.unrequested_line_count")
    assert (extras.operator, extras.value) == (Operator.EQ, 0)


def test_session_instruction_becomes_a_check_not_just_advice(instructions):
    policy = compile_policy(instructions["SCEN0003"])
    session = rule_for(policy, "derived.session_integrity")
    assert (session.operator, session.value) == (Operator.NE, "degraded")


def test_familiar_shop_wording_is_recognised_across_phrasings(instructions):
    for scenario in ("SCEN0000", "SCEN0003", "SCEN0004"):
        policy = compile_policy(instructions[scenario])
        assert rule_for(policy, "derived.merchant_familiarity") is not None, scenario


def test_ask_me_when_uncertain_sets_the_customers_own_fallback(instructions):
    for text in instructions.values():
        assert compile_policy(text).uncertainty_policy is UncertaintyPolicy.ASK


def test_no_stated_preference_still_defaults_to_asking():
    """Asking neither spends the money nor blocks the shopping on our guess."""
    policy = compile_policy("Buy groceries up to CHF 50.")
    assert policy.uncertainty_policy is UncertaintyPolicy.ASK


# --- what the compiler refuses to decide alone -----------------------------


def test_undefined_familiarity_threshold_is_raised_not_guessed(instructions):
    """'A shop I use regularly' has no field in the event. Somebody must say
    what 'regularly' means, and it is not us."""
    policy = compile_policy(instructions["SCEN0000"])
    assert any("familiar" in q.lower() for q in policy.open_questions)


def test_split_order_gap_is_raised_against_any_per_order_limit(instructions):
    """Two orders minutes apart can each stay under a per-order cap while
    together exceeding it. The customer should rule on that, not the engine."""
    policy = compile_policy(instructions["SCEN0001"])
    assert any("minutes apart" in q for q in policy.open_questions)


def test_specialist_retailer_reading_is_put_back_to_the_customer(instructions):
    policy = compile_policy(instructions["SCEN0002"])
    assert any("specialist" in q.lower() for q in policy.open_questions)


def test_unstated_return_window_is_raised_as_a_question(instructions):
    policy = compile_policy(instructions["SCEN0002"])
    assert any("does not state a return window" in q for q in policy.open_questions)


def test_an_instruction_that_authorises_nothing_says_so():
    policy = compile_policy("Do your best for me.")
    assert not policy.hard_rules
    assert policy.open_questions
    assert "could not turn this into any executable check" in policy.open_questions[0]


def test_a_spending_floor_is_questioned_rather_than_inverted():
    policy = compile_policy("Spend at least CHF 500 on groceries.")
    assert not any(r.field == "authorization.billing_amount_chf" for r in policy.hard_rules)
    assert any("minimum" in q for q in policy.open_questions)


def test_more_than_amount_is_a_blocking_floor_not_an_upper_limit():
    policy = compile_policy(
        "Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. "
        "Ask me when uncertain. More than CHF 30"
    )
    amount_rules = rules_for(policy, "authorization.billing_amount_chf")

    assert [(rule.operator, rule.value) for rule in amount_rules] == [(Operator.LTE, 20.0)]
    assert any(
        question.startswith("Policy conflict:")
        and "more than CHF 30" in question
        and "caps the purchase at CHF 20" in question
        for question in policy.open_questions
    )


@pytest.mark.parametrize(
    ("instruction", "fragment"),
    [
        ("Buy only groceries. Also buy electronics.", "categories should actually be allowed"),
        ("Ask me when uncertain. Decline when uncertain.", "both to ask and to decline"),
        ("Buy one grocery item. Buy two grocery items.", "incompatible purchase quantities"),
        ("Buy only in Switzerland. Also buy in Germany.", "which countries are allowed"),
        ("Buy only from Migros. Also buy from Coop.", "which merchants are allowed"),
        (
            "Keep each order under CHF 100 and total weekly spend under CHF 50.",
            "per-purchase limit",
        ),
        ("Allow no subscriptions. Buy a monthly software subscription.", "recurring purchases"),
    ],
)
def test_known_policy_conflicts_are_blocking_questions(instruction, fragment):
    policy = compile_policy(instruction)
    conflicts = [q for q in policy.open_questions if q.startswith("Policy conflict:")]
    assert any(fragment in question.lower() for question in conflicts), conflicts


def test_a_non_chf_limit_is_flagged():
    policy = compile_policy("Buy books for up to EUR 40 each. Ask me when uncertain.")
    assert any("EUR" in q for q in policy.open_questions)


def test_questions_and_guidance_are_not_repeated():
    policy = compile_policy(
        "Buy groceries under CHF 50 from a shop I use regularly, "
        "from shops I have used before. Ask me when uncertain."
    )
    assert len(policy.guidance) == len(set(policy.guidance))
    assert len(policy.open_questions) == len(set(policy.open_questions))


def test_draft_payload_is_the_documented_request_body(instructions):
    payload = compile_policy(instructions["SCEN0000"]).to_draft_payload()
    assert set(payload) == {
        "instruction",
        "hard_rules",
        "uncertainty_policy",
        "guidance",
        "open_questions",
    }
    assert payload["instruction"] == instructions["SCEN0000"]
    assert payload["uncertainty_policy"] == "ask"
    assert "customer_id" not in payload and "card_id" not in payload


def test_compilation_is_deterministic(instructions):
    """The same sentence must produce the same rules every time."""
    for text in instructions.values():
        first, second = compile_policy(text), compile_policy(text)
        assert [r.identity() for r in first.hard_rules] == [r.identity() for r in second.hard_rules]
        assert first.open_questions == second.open_questions


# --- amendments may only tighten -------------------------------------------


def money_rule(limit: float) -> CompiledRule:
    return CompiledRule(
        "authorization.billing_amount_chf", Operator.LTE, limit, Currency.CHF, RuleScope.PURCHASE
    )


def test_removing_a_rule_is_refused():
    existing = [money_rule(120.0), CompiledRule("item.item_category", Operator.IN, ["groceries"])]
    review = review_amendment(existing, existing[:1], current_policy=UncertaintyPolicy.ASK)
    assert not review.allowed
    assert "would be removed" in review.problems[0]


def test_adding_a_tighter_limit_is_allowed_and_explained():
    existing = [money_rule(120.0)]
    review = review_amendment(
        existing, existing + [money_rule(80.0)], current_policy=UncertaintyPolicy.ASK
    )
    assert review.allowed
    assert any("now governs" in note for note in review.notes)


def test_adding_a_looser_limit_changes_nothing_and_says_so():
    """Rules combine with AND, so a looser addition cannot raise the ceiling.
    The customer is told that rather than left believing they raised it."""
    existing = [money_rule(120.0)]
    review = review_amendment(
        existing, existing + [money_rule(200.0)], current_policy=UncertaintyPolicy.ASK
    )
    assert review.allowed
    assert any("still governs" in note for note in review.notes)


def test_contradictory_rules_are_refused():
    existing = [money_rule(120.0)]
    floor = CompiledRule(
        "authorization.billing_amount_chf", Operator.GTE, 500.0, Currency.CHF, RuleScope.PURCHASE
    )
    review = review_amendment(existing, existing + [floor], current_policy=UncertaintyPolicy.ASK)
    assert not review.allowed
    assert "block every purchase" in review.problems[0]


def test_disjoint_allow_lists_are_refused():
    existing = [CompiledRule("item.item_category", Operator.IN, ["groceries"])]
    other = CompiledRule("item.item_category", Operator.IN, ["electronics"])
    review = review_amendment(existing, existing + [other], current_policy=UncertaintyPolicy.ASK)
    assert not review.allowed


@pytest.mark.parametrize(
    ("current", "new", "allowed"),
    [
        (UncertaintyPolicy.ASK, UncertaintyPolicy.DECLINE, True),
        (UncertaintyPolicy.APPROVE, UncertaintyPolicy.DECLINE, True),
        (UncertaintyPolicy.ASK, UncertaintyPolicy.ASK, True),
        (UncertaintyPolicy.APPROVE, UncertaintyPolicy.ASK, False),
        (UncertaintyPolicy.ASK, UncertaintyPolicy.APPROVE, False),
        (UncertaintyPolicy.DECLINE, UncertaintyPolicy.ASK, False),
    ],
)
def test_uncertainty_policy_only_moves_toward_decline(current, new, allowed):
    existing = [money_rule(120.0)]
    review = review_amendment(
        existing, existing, current_policy=current, new_policy=new
    )
    assert review.allowed is allowed


def test_tightness_comparison_on_allow_lists():
    narrow = CompiledRule("item.item_category", Operator.IN, ["groceries"])
    wide = CompiledRule("item.item_category", Operator.IN, ["groceries", "household"])
    assert binds_more_tightly(narrow, wide) is True
    assert binds_more_tightly(wide, narrow) is False


def test_rules_on_different_fields_are_not_compared():
    assert binds_more_tightly(money_rule(120.0), CompiledRule("cart.line_count", Operator.LTE, 3)) is None
    assert not is_unsatisfiable(money_rule(120.0), CompiledRule("cart.line_count", Operator.LTE, 3))


def test_a_period_limit_is_not_compared_against_a_per_order_limit():
    """CHF 300 across seven days does not conflict with CHF 120 per order."""
    per_order = money_rule(120.0)
    period = CompiledRule(
        "derived.spend_in_period_chf", Operator.LTE, 300.0, Currency.CHF, RuleScope.PERIOD, 7
    )
    assert binds_more_tightly(period, per_order) is None
    assert not is_unsatisfiable(period, per_order)


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("Buy me black running shoes for up to CHF 200.", "black running shoes"),
        ("Buy us a laptop stand from a shop I use regularly.", "laptop stand"),
        ("Buy a 27-inch monitor under CHF 400.", "27-inch monitor"),
        ("Buy the 27-inch monitor I chose for CHF 400 or less.", "27-inch monitor"),
        ("Replace my worn road-running shoes in size 43.", "road-running shoes"),
        # A category word names a kind of thing, so the item_category rule
        # already covers it and no literal phrase is stored.
        ("Buy us groceries for delivery under CHF 80.", None),
        ("The agent may buy clothing for me, up to CHF 250 per order.", None),
    ],
)
def test_the_requested_item_excludes_who_it_is_for_and_where_from(instruction, expected):
    """"Buy me shoes from X" asks for shoes — not for "me shoes from X"."""
    policy = compile_policy(instruction)
    rule = rule_for(policy, "derived.requested_item")
    assert (rule.value if rule else None) == expected
