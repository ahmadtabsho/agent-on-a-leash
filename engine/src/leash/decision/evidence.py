"""What the engine saw, and what it concluded.

Judges have to understand what the system permitted, what evidence it
considered, and why it acted. So a verdict is never a bare decision: every
stage appends findings, and the decision is derived from them. If a finding
does not appear here, it did not influence the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum

from ..models.enums import Decision, UncertaintyPolicy


class Outcome(str, Enum):
    """What a single check concluded."""

    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"
    """We could not establish the fact. Not the same as failing it."""

    NOT_APPLICABLE = "not_applicable"


class Stage(str, Enum):
    PRECONDITIONS = "preconditions"
    SANITISE = "sanitise"
    FACTS = "facts"
    HARD_RULES = "hard_rules"
    SIGNALS = "signals"
    RESOLVE = "resolve"


@dataclass(frozen=True)
class Finding:
    """One observation, in the customer's terms as well as the engine's."""

    stage: Stage
    code: str
    outcome: Outcome
    detail: str
    field: str | None = None
    observed: str | None = None
    expected: str | None = None

    @property
    def blocks(self) -> bool:
        return self.outcome is Outcome.FAIL

    @property
    def unsettles(self) -> bool:
        return self.outcome is Outcome.UNCERTAIN

    def to_payload(self) -> dict:
        payload = {"code": self.code, "outcome": self.outcome.value, "detail": self.detail}
        for key, value in (
            ("field", self.field),
            ("observed", self.observed),
            ("expected", self.expected),
        ):
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class ClarificationChoice:
    """One answer the customer may give to a focused purchase question."""

    id: str
    label: str
    decision: Decision

    def to_payload(self) -> dict:
        return {"id": self.id, "label": self.label, "decision": self.decision.value}


@dataclass(frozen=True)
class Clarification:
    """A bounded question produced for an uncertain purchase."""

    question: str
    finding_code: str
    choices: tuple[ClarificationChoice, ...]
    answer_scope: str = "purchase"
    generated_by: str = "code"
    latency_ms: float = 0.0
    fallback_used: bool = True
    status: str = "model_unavailable"

    def to_payload(self) -> dict:
        return {
            "question": self.question,
            "finding_code": self.finding_code,
            "choices": [choice.to_payload() for choice in self.choices],
            "answer_scope": self.answer_scope,
            "generated_by": self.generated_by,
            "latency_ms": round(self.latency_ms, 1),
            "fallback_used": self.fallback_used,
            "status": self.status,
        }


@dataclass
class Verdict:
    """The engine's answer, with the reasoning attached."""

    decision: Decision
    findings: list[Finding] = dc_field(default_factory=list)
    reason_codes: list[str] = dc_field(default_factory=list)
    customer_message: str = ""
    engine_version: str = ""
    # Set when the customer's uncertainty_policy, rather than a definite
    # breach, produced this decision.
    fell_back_to_policy: UncertaintyPolicy | None = None
    clarification: Clarification | None = None
    elapsed_ms: float = 0.0

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocks]

    @property
    def unsettled(self) -> list[Finding]:
        return [f for f in self.findings if f.unsettles]

    def to_decision_payload(self, authorization_id: str) -> dict:
        """Body for `POST /v1/authorizations/{id}/decision`."""
        payload: dict = {
            "authorization_id": authorization_id,
            "decision": self.decision.value,
            "reason_codes": list(self.reason_codes),
        }
        if self.customer_message:
            payload["customer_message"] = self.customer_message
        evidence = [f.to_payload() for f in self.findings if f.outcome is not Outcome.PASS]
        if evidence:
            payload["evidence"] = evidence
        if self.engine_version:
            payload["engine_version"] = self.engine_version
        return payload


class Ledger:
    """Collects findings as the stages run."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(
        self,
        stage: Stage,
        code: str,
        outcome: Outcome,
        detail: str,
        *,
        field: str | None = None,
        observed: str | None = None,
        expected: str | None = None,
    ) -> Finding:
        finding = Finding(stage, code, outcome, detail, field, observed, expected)
        self.findings.append(finding)
        return finding

    @property
    def has_blocker(self) -> bool:
        return any(f.blocks for f in self.findings)

    @property
    def has_uncertainty(self) -> bool:
        return any(f.unsettles for f in self.findings)
