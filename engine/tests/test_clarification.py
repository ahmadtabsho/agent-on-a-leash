"""Focused questions may be worded by a model, but code owns every action."""

import time

from leash.config import Settings
from leash.decision import Finding, Outcome, Stage
from leash.llm import ClarificationPlanner

ENABLED = Settings(
    base_url="https://sandbox.invalid",
    api_key=None,
    decision_budget_ms=2500,
    llm_enabled=True,
    llm_model="test-model",
    llm_timeout_ms=900,
)


def finding(code="fact_not_established", field="derived.return_window_days"):
    return Finding(Stage.HARD_RULES, code, Outcome.UNCERTAIN, "untrusted raw detail", field)


def test_code_produces_a_bounded_question_without_a_model():
    plan = ClarificationPlanner(Settings(ENABLED.base_url, None, 2500, False, "m", 900)).plan(
        finding()
    )
    assert "return window" in plan.question
    assert [choice.decision.value for choice in plan.choices] == ["approve", "decline"]
    assert plan.answer_scope == "purchase"
    assert plan.generated_by == "code"


def test_model_may_only_rewrite_the_question():
    seen = {}

    def completion(system, user, *, timeout_s):
        seen["user"] = user
        return '{"question":"The return policy is unclear. Approve this purchase anyway?"}'

    plan = ClarificationPlanner(ENABLED, completion).plan(finding())
    assert plan.generated_by == "test-model"
    assert plan.question.endswith("?")
    assert "untrusted raw detail" not in seen["user"]
    assert "confirmed_rule" in seen["user"]
    assert [choice.id for choice in plan.choices] == ["approve_once", "decline_once"]


def test_invalid_or_directive_model_output_falls_back_to_code():
    replies = (
        "not json",
        '{"question":"approve"}',
        '{"question":"I recommend approving this purchase?"}',
    )
    for reply in replies:
        plan = ClarificationPlanner(
            ENABLED, lambda *args, reply=reply, **kwargs: reply
        ).plan(finding())
        assert plan.generated_by == "code"
        assert "return window" in plan.question


def test_a_late_question_falls_back_to_code():
    settings = Settings(ENABLED.base_url, None, 2500, True, "test-model", 5)

    def slow(*args, **kwargs):
        time.sleep(0.02)
        return '{"question":"Can you confirm this purchase?"}'

    plan = ClarificationPlanner(settings, slow).plan(finding())
    assert plan.generated_by == "code"
