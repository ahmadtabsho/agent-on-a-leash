"""Resolve policy ambiguities before the customer can activate a mandate."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..config import Settings
from .advisor import Completion, completion_from_settings

QUESTION_PROMPT = """Rewrite one policy clarification question for a customer.

Keep exactly the same issue and quoted amounts. Use one short, neutral question.
Do not answer it or recommend an option. Reply with JSON only:
{"question": "..."}
"""

REVISION_PROMPT = """Rewrite a customer's shopping permission after clarification.

Preserve every explicit constraint that the customer's answers do not change.
Resolve each listed question exactly as the customer answered it. Produce a
single clear instruction suitable for deterministic rule extraction. Never add
a permission, product, merchant, amount, or exception that the customer did not
state. Reply with JSON only: {"instruction": "..."}
"""


@dataclass(frozen=True)
class PolicyQuestion:
    id: str
    question: str
    blocking: bool
    generated_by: str

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "blocking": self.blocking,
            "generated_by": self.generated_by,
        }


@dataclass(frozen=True)
class AIActivity:
    model: str
    task: str
    result: str
    latency_ms: float
    fallback_used: bool
    status: str

    def to_payload(self) -> dict:
        return {
            "model": self.model,
            "task": self.task,
            "result": self.result,
            "latency_ms": round(self.latency_ms, 1),
            "fallback_used": self.fallback_used,
            "status": self.status,
        }


@dataclass(frozen=True)
class PolicyReview:
    questions: list[PolicyQuestion]
    activity: list[AIActivity]


def _is_blocking(question: str) -> bool:
    lowered = question.lower()
    return (
        lowered.startswith("policy conflict:")
        or "reads as a minimum" in lowered
        or "could not turn this into any executable check" in lowered
    )


class PolicyClarifier:
    """Build questions from compiler findings and revise only from user answers."""

    def __init__(self, settings: Settings | None = None, completion: Completion | None = None):
        self.settings = settings or Settings.from_env()
        self._completion = completion
        if self._completion is None:
            self._completion = completion_from_settings(self.settings, max_tokens=500)

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_enabled and self._completion)

    def review(
        self,
        open_questions: list[str],
        *,
        use_ai: bool = True,
        failure_mode: str = "none",
    ) -> PolicyReview:
        result: list[PolicyQuestion] = []
        activity: list[AIActivity] = []
        for index, source in enumerate(open_questions):
            blocking = _is_blocking(source)
            question, generated_by = source, "code"
            if blocking:
                started = time.perf_counter()
                status = self._unavailable_status(use_ai, failure_mode)
                rewritten = None
                if status == "ok":
                    rewritten = self._complete(
                        QUESTION_PROMPT, {"draft_question": source}, "question"
                    )
                    if rewritten is None:
                        status = "invalid_or_failed_response"
                if rewritten and rewritten.endswith("?"):
                    question, generated_by = rewritten, self.settings.llm_model
                activity.append(
                    AIActivity(
                        model=self.settings.llm_model,
                        task="Policy clarification",
                        result=(
                            "Reworded conflict question"
                            if generated_by != "code"
                            else "Used deterministic conflict question"
                        ),
                        latency_ms=(time.perf_counter() - started) * 1000,
                        fallback_used=generated_by == "code",
                        status=status,
                    )
                )
            result.append(PolicyQuestion(f"policy-q-{index + 1}", question, blocking, generated_by))
        return PolicyReview(result, activity)

    def questions(self, open_questions: list[str]) -> list[PolicyQuestion]:
        return self.review(open_questions).questions

    def revise(
        self,
        instruction: str,
        questions: list[PolicyQuestion],
        answers: dict[str, str],
    ) -> str | None:
        revised, _ = self.revise_with_activity(instruction, questions, answers)
        return revised

    def revise_with_activity(
        self,
        instruction: str,
        questions: list[PolicyQuestion],
        answers: dict[str, str],
        *,
        use_ai: bool = True,
        failure_mode: str = "none",
    ) -> tuple[str | None, AIActivity]:
        started = time.perf_counter()
        status = self._unavailable_status(use_ai, failure_mode)
        if status != "ok":
            return None, AIActivity(
                self.settings.llm_model,
                "Policy rewrite",
                "Kept original instruction",
                (time.perf_counter() - started) * 1000,
                True,
                status,
            )
        answered = [
            {"question": question.question, "answer": answers[question.id]}
            for question in questions
            if question.id in answers and answers[question.id].strip()
        ]
        if not answered:
            return None, AIActivity(
                self.settings.llm_model,
                "Policy rewrite",
                "Kept original instruction",
                (time.perf_counter() - started) * 1000,
                True,
                "no_answers",
            )
        revised = self._complete(
            REVISION_PROMPT,
            {"original_instruction": instruction, "clarifications": answered},
            "instruction",
        )
        if revised is None:
            return None, AIActivity(
                self.settings.llm_model,
                "Policy rewrite",
                "Kept original instruction",
                (time.perf_counter() - started) * 1000,
                True,
                "invalid_or_failed_response",
            )
        revised = " ".join(revised.split()).strip()
        valid = revised if 1 <= len(revised) <= 2000 else None
        return valid, AIActivity(
            self.settings.llm_model,
            "Policy rewrite",
            "Reworded instruction" if valid else "Kept original instruction",
            (time.perf_counter() - started) * 1000,
            valid is None,
            "ok" if valid else "invalid_or_failed_response",
        )

    def _unavailable_status(self, use_ai: bool, failure_mode: str) -> str:
        if not use_ai:
            return "disabled"
        if failure_mode in {"timeout", "invalid_response", "missing_key"}:
            return f"simulated_{failure_mode}"
        if not self.available:
            return "missing_key_or_unavailable"
        return "ok"

    def _complete(self, system: str, payload: dict, field: str) -> str | None:
        try:
            raw = self._completion(
                system,
                json.dumps(payload),
                timeout_s=max(2.0, self.settings.llm_timeout_ms / 1000),
            )
            parsed = json.loads((raw or "").strip())
        except Exception:  # noqa: BLE001 - optional model failures are a normal fallback
            return None
        value = parsed.get(field) if isinstance(parsed, dict) else None
        if not isinstance(value, str):
            return None
        value = " ".join(value.split()).strip()
        return value if 1 <= len(value) <= 2000 else None
