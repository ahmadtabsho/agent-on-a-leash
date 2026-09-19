"""Policy ambiguity is resolved before confirmation, never during spending."""

from leash.config import Settings
from leash.llm import PolicyClarifier

ENABLED = Settings("https://sandbox.invalid", None, 2500, True, "test-model", 1200)


def completion(system, user, *, timeout_s):
    if "Rewrite one policy clarification" in system:
        return '{"question":"You mentioned CHF 30 or more. What limit did you intend?"}'
    return '{"instruction":"Buy one grocery item for CHF 20 or less from a familiar shop. Ask me when uncertain."}'


def test_only_questions_that_block_confirmation_are_sent_to_the_model():
    clarifier = PolicyClarifier(ENABLED, completion)
    questions = clarifier.questions(
        [
            '"Buy an item for CHF 30 or more" reads as a minimum of CHF 30. Did you mean that as a limit instead?',
            "How many past purchases make a shop familiar to you — one, or a few?",
        ]
    )
    assert questions[0].blocking
    assert questions[0].generated_by == "test-model"
    assert "CHF 30" in questions[0].question
    assert not questions[1].blocking
    assert questions[1].generated_by == "code"


def test_customer_answers_are_rewritten_into_a_fresh_instruction():
    clarifier = PolicyClarifier(ENABLED, completion)
    questions = clarifier.questions(
        ['"CHF 30 or more" reads as a minimum of CHF 30. Did you mean that as a limit instead?']
    )
    revised = clarifier.revise(
        "Buy groceries for CHF 20 or less. Buy an item for CHF 30 or more.",
        questions,
        {questions[0].id: "The CHF 30 sentence was a mistake; keep the CHF 20 maximum."},
    )
    assert revised == (
        "Buy one grocery item for CHF 20 or less from a familiar shop. "
        "Ask me when uncertain."
    )
