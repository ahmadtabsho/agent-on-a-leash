"""Turn structured uncertainty into one bounded customer question.

Code chooses when to ask and which answers are permitted. The optional model
may only improve the wording of a safe question assembled here. It never sees
merchant prose, changes a rule, or chooses a decision.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

from ..config import Settings
from ..models.enums import Decision
from .advisor import Completion, _openrouter_completion

if TYPE_CHECKING:
    from ..decision.evidence import Clarification, Finding

SYSTEM_PROMPT = """Rewrite a purchase clarification question for a customer.

Keep the same meaning. Use one short, neutral sentence ending in a question.
Do not recommend an answer, add facts, mention internal codes, or include answer
choices. Reply with JSON only: {"question": "..."}
"""


def _base_question(finding: Finding) -> str:
    by_code = {
        "merchant_text_targets_decider": (
            "The seller's product text appears to contain instructions for an automated buyer. "
            "Do you want to approve this purchase anyway?"
        ),
        "order_text_targets_decider": (
            "The order description appears to contain instructions for an automated buyer. "
            "Do you want to approve this purchase anyway?"
        ),
        "lookalike_merchant": (
            "This unfamiliar shop has a name similar to one you use. "
            "Do you recognize and approve this shop?"
        ),
        "possible_duplicate_order": (
            "This looks like a recent order for the same basket and amount. "
            "Did you intend to buy it again?"
        ),
        "unrequested_addition": (
            "The basket contains something you did not request. "
            "Do you want to approve the whole basket?"
        ),
        "recurring_commitment": (
            "This purchase appears to create a recurring charge. "
            "Do you want to approve that commitment?"
        ),
        "totals_do_not_reconcile": (
            "The seller's line items and delivery fee do not match the order total. "
            "Do you want to approve this amount?"
        ),
        "billing_amount_does_not_reconcile": (
            "The converted amount does not match the stated CHF total. "
            "Do you want to approve this amount?"
        ),
        "intent_match_disputed": (
            "The item may not be the product you requested. "
            "Do you want to approve it anyway?"
        ),
        "no_rules_confirmed": (
            "There are no confirmed checks for this purchase. "
            "Do you want to approve it once?"
        ),
    }
    if finding.code in by_code:
        return by_code[finding.code]

    by_field = {
        "derived.return_window_days": (
            "The required return window could not be confirmed. "
            "Do you want to approve this purchase anyway?"
        ),
        "authorization.order_returnable": (
            "The seller did not clearly confirm that this order can be returned. "
            "Do you want to approve it anyway?"
        ),
        "derived.requested_attribute": (
            "A requested product detail could not be confirmed. "
            "Do you want to approve this item anyway?"
        ),
        "derived.requested_item_match": (
            "The basket could not be clearly matched to the product you requested. "
            "Do you want to approve it anyway?"
        ),
        "derived.session_integrity": (
            "This shopping session looks unusual. "
            "Do you recognize and approve this purchase?"
        ),
    }
    return by_field.get(
        finding.field or "",
        "One of your purchase rules could not be confirmed. Do you want to approve this purchase once?",
    )


class ClarificationPlanner:
    """Produces a question even when the optional model is unavailable."""

    def __init__(self, settings: Settings | None = None, completion: Completion | None = None):
        self.settings = settings or Settings.from_env()
        self._completion = completion
        if self._completion is None and self.settings.llm_enabled:
            key = os.environ.get("OPENROUTER_API_KEY")
            if key:
                self._completion = _openrouter_completion(self.settings.llm_model, key)

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_enabled and self._completion)

    def plan(self, finding: Finding) -> Clarification:
        from ..decision.evidence import Clarification, ClarificationChoice

        base = _base_question(finding)
        question, generated_by = base, "code"
        started = time.perf_counter()
        status = "model_unavailable"
        if self.available:
            rewritten = self._rewrite(finding, base)
            if rewritten:
                question, generated_by = rewritten, self.settings.llm_model
                status = "ok"
            else:
                status = "invalid_or_failed_response"

        choices = (
            ClarificationChoice("approve_once", "Yes, approve this purchase", Decision.APPROVE),
            ClarificationChoice("decline_once", "No, decline this purchase", Decision.DECLINE),
        )
        return Clarification(
            question,
            finding.code,
            choices,
            generated_by=generated_by,
            latency_ms=(time.perf_counter() - started) * 1000,
            fallback_used=generated_by == "code",
            status=status,
        )

    def _rewrite(self, finding: Finding, base: str) -> str | None:
        # The model receives the safe template and structured identifiers only.
        # Finding.detail may contain untrusted merchant prose and is excluded.
        user = json.dumps(
            {
                "finding_code": finding.code,
                "field": finding.field,
                "confirmed_rule": finding.expected,
                "draft_question": base,
            }
        )
        started = time.perf_counter()
        try:
            raw = self._completion(
                SYSTEM_PROMPT, user, timeout_s=self.settings.llm_timeout_ms / 1000
            )
        except Exception:  # noqa: BLE001 - optional model failures are non-fatal
            return None
        if (time.perf_counter() - started) * 1000 > self.settings.llm_timeout_ms:
            return None
        try:
            payload = json.loads((raw or "").strip())
        except (json.JSONDecodeError, TypeError):
            return None
        question = payload.get("question") if isinstance(payload, dict) else None
        if not isinstance(question, str):
            return None
        question = " ".join(question.split()).strip()
        if not question.endswith("?") or not 10 <= len(question) <= 240:
            return None
        lowered = question.lower()
        if any(
            phrase in lowered
            for phrase in ("i recommend", "we recommend", "definitely", "safe to approve")
        ):
            return None
        return question
