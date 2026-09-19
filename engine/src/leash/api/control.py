"""The control plane the customer's interface talks to.

Separate from the decision engine on purpose. Viseca intends to fold this
surface into the existing `one` app while the decision runs as a backend hot
path, so the two are different deployables that share only this contract. The
engine never imports anything from here; this module calls into the engine.

It runs in two modes. With a team key it proxies the sandbox. Without one it
runs the vendored scenarios locally, which is what makes the whole flow
demonstrable before the event day.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..config import Settings
from ..decision import DecisionEngine, RunState
from ..decision.state import Recorded
from ..llm import ClarificationPlanner, IntentAdvisor, PolicyClarifier, PolicyQuestion
from ..models.enums import Decision, UncertaintyPolicy
from ..policy import CompiledPolicy, compile_policy, review_amendment
from ..replay import DecisionLog, DecisionRecord, EventBuilder
from .store import SessionStore

ENGINE = DecisionEngine()
POLICY_CLARIFIER = PolicyClarifier()
BUILDER = EventBuilder()

# The customer's window to answer a paused purchase. The live service reports
# 120 seconds; this mirrors it so the interface can show a truthful countdown.
# LEASH_HUMAN_TIMEOUT_SECONDS shortens it for demonstration — never lengthens
# it, because promising time the platform will not honour would be a lie.
HUMAN_TIMEOUT_SECONDS = min(120.0, Settings.from_env().human_timeout_override_s or 120.0)


# --- session ---------------------------------------------------------------


@dataclass
class Mandate:
    """A policy in the customer's hands: drafted, confirmed, tightened, revoked."""

    draft_id: str
    instruction: str
    hard_rules: list[dict]
    guidance: list[str]
    open_questions: list[str]
    uncertainty_policy: str
    policy_questions: list[dict] = dc_field(default_factory=list)
    ai_activity: list[dict] = dc_field(default_factory=list)
    status: str = "draft"
    mandate_id: str | None = None
    confirmed_at: str | None = None
    revoked_at: str | None = None
    amendments: list[dict] = dc_field(default_factory=list)


@dataclass
class DemoSettings:
    """Session-level controls for explaining and demonstrating optional AI."""

    ai_enabled: bool = True
    show_ai_activity: bool = True
    ai_failure_mode: str = "none"


@dataclass
class Session:
    """Everything one customer's session holds. In-memory by design."""

    mandate: Mandate | None = None
    compiled: CompiledPolicy | None = None
    runs: dict[str, dict] = dc_field(default_factory=dict)
    pending: dict[str, dict] = dc_field(default_factory=dict)
    lapsed: dict[str, dict] = dc_field(default_factory=dict)
    log: DecisionLog = dc_field(default_factory=DecisionLog)
    settings: DemoSettings = dc_field(default_factory=lambda: DemoSettings())


STORE = SessionStore()


def _restore() -> Session:
    """Rebuild the session from disk, or start a fresh one.

    A restart used to leave the customer with no policy and no record that
    anything was waiting on them, while the decisions already sent stayed on
    the platform. Anything that cannot be read back is discarded rather than
    half-restored.
    """
    payload = STORE.load()
    session = Session()
    if not payload:
        return session

    if payload.get("mandate"):
        try:
            session.mandate = Mandate(**payload["mandate"])
        except TypeError:
            return Session()

    instruction = payload.get("instruction")
    if instruction:
        session.compiled = compile_policy(instruction)

    session.pending = payload.get("pending") or {}
    session.lapsed = payload.get("lapsed") or {}
    if payload.get("settings"):
        try:
            session.settings = DemoSettings(**payload["settings"])
        except TypeError:
            session.settings = DemoSettings()

    # Runs come back for display. Their in-memory RunState is rebuilt from the
    # journal below, so spending limits carry across the restart.
    for scenario_id, run in (payload.get("runs") or {}).items():
        session.runs[scenario_id] = {**run, "state": RunState(run_id=scenario_id)}

    for entry in payload.get("journal") or []:
        try:
            record = DecisionRecord(**entry)
        except TypeError:
            continue
        session.log.records.append(record)
        run = session.runs.get(record.run_id)
        if run is None:
            continue
        state: RunState = run["state"]
        state.records[record.authorization_id] = Recorded(
            authorization_id=record.authorization_id,
            decision=Decision(record.resolved_by_customer or record.decision),
            billing_amount_chf=Decimal(str(record.billing_amount_chf)),
            timestamp=datetime.fromisoformat(record.purchase_timestamp.replace("Z", "+00:00")),
            merchant_id=record.merchant_id,
            fingerprint="",
            awaiting_customer=(
                record.decision == Decision.STEP_UP.value and not record.resolved_by_customer
            ),
        )
    return session


SESSION = _restore()




def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _policy_payload(
    policy: CompiledPolicy,
    policy_questions: list[dict] | None = None,
    ai_activity: list[dict] | None = None,
) -> dict:
    return {
        "instruction": policy.instruction,
        "uncertainty_policy": policy.uncertainty_policy.value,
        "guidance": policy.guidance,
        "open_questions": policy.open_questions,
        "policy_questions": policy_questions or [],
        "ai_activity": ai_activity or [],
        "hard_rules": [
            {**rule.to_payload(), "source": rule.source} for rule in policy.hard_rules
        ],
    }


def _require_active() -> Mandate:
    mandate = SESSION.mandate
    if mandate is None:
        raise HTTPException(409, "No policy has been created yet.")
    if mandate.status != "active":
        raise HTTPException(409, f"The policy is {mandate.status}, not active.")
    return mandate


def _demo_engine() -> DecisionEngine:
    """Apply session demo controls without mutating the production engine."""
    if SESSION.settings.ai_enabled and SESSION.settings.ai_failure_mode == "none":
        return ENGINE
    current = Settings.from_env()
    disabled = Settings(
        current.base_url,
        current.api_key,
        current.decision_budget_ms,
        False,
        current.llm_model,
        current.llm_timeout_ms,
    )
    return DecisionEngine(
        advisor=IntentAdvisor(disabled),
        clarifier=ClarificationPlanner(disabled),
    )


# --- request bodies --------------------------------------------------------


class InstructionIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)


class ConfirmIn(BaseModel):
    confirmed: bool = True


class TightenIn(BaseModel):
    uncertainty_policy: str | None = None
    add_rules: list[dict] = Field(default_factory=list)


class ResolveIn(BaseModel):
    decision: str
    message: str = ""


class PolicyAnswerIn(BaseModel):
    question_id: str
    answer: str = Field(min_length=1, max_length=1000)


class PolicyRefineIn(BaseModel):
    answers: list[PolicyAnswerIn] = Field(min_length=1)


class DemoSettingsIn(BaseModel):
    ai_enabled: bool
    show_ai_activity: bool
    ai_failure_mode: Literal["none", "timeout", "invalid_response", "missing_key"]


# --- app -------------------------------------------------------------------

app = FastAPI(
    title="Agent on a Leash — wallet control",
    description="The customer's control surface. Deploys independently of the decision engine.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    settings = Settings.from_env()
    return {
        "status": "ok",
        "engine_version": ENGINE.config.engine_version,
        "session_restored": SESSION.mandate is not None,
        "session_file": str(STORE.path),
        "sandbox_base_url": settings.base_url,
        "team_key_configured": bool(settings.api_key),
        "mode": "sandbox" if settings.api_key else "offline",
        "advisor_enabled": bool(ENGINE.advisor and ENGINE.advisor.available),
        "clarifier_enabled": ENGINE.clarifier.available,
        "policy_clarifier_enabled": POLICY_CLARIFIER.available,
        "demo_settings": asdict(SESSION.settings),
        "scenarios": [
            {
                "scenario_id": sid,
                "name": BUILDER.catalogue[sid]["scenario_name"],
                "instruction": BUILDER.catalogue[sid]["cardholder_instruction"],
                "events": int(BUILDER.catalogue[sid]["event_count"]),
            }
            for sid in BUILDER.scenario_ids()
        ],
    }


# --- policy ----------------------------------------------------------------


@app.get("/api/settings")
def read_settings() -> dict:
    return asdict(SESSION.settings)


@app.put("/api/settings")
def update_settings(body: DemoSettingsIn) -> dict:
    SESSION.settings = DemoSettings(**body.model_dump())
    STORE.save(SESSION)
    return asdict(SESSION.settings)


@app.post("/api/policy/preview")
def preview_policy(body: InstructionIn) -> dict:
    """Show what an instruction would become, before anything is authorised."""
    policy = compile_policy(body.instruction)
    review = POLICY_CLARIFIER.review(
        policy.open_questions,
        use_ai=SESSION.settings.ai_enabled,
        failure_mode=SESSION.settings.ai_failure_mode,
    )
    questions = [q.to_payload() for q in review.questions]
    activity = [item.to_payload() for item in review.activity]
    return _policy_payload(policy, questions, activity)


def _store_draft(policy: CompiledPolicy, prior_activity: list[dict] | None = None) -> dict:
    review = POLICY_CLARIFIER.review(
        policy.open_questions,
        use_ai=SESSION.settings.ai_enabled,
        failure_mode=SESSION.settings.ai_failure_mode,
    )
    questions = [q.to_payload() for q in review.questions]
    activity = list(prior_activity or []) + [item.to_payload() for item in review.activity]
    SESSION.compiled = policy
    SESSION.mandate = Mandate(
        draft_id=f"DRAFT-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        instruction=policy.instruction,
        hard_rules=[r.to_payload() for r in policy.hard_rules],
        guidance=policy.guidance,
        open_questions=policy.open_questions,
        uncertainty_policy=policy.uncertainty_policy.value,
        policy_questions=questions,
        ai_activity=activity,
    )
    STORE.save(SESSION)
    return {
        "mandate": asdict(SESSION.mandate),
        "policy": _policy_payload(policy, questions, activity),
    }


@app.post("/api/policy/draft")
def draft_policy(body: InstructionIn) -> dict:
    """Create a draft. Nothing is authorised until the customer confirms."""
    return _store_draft(compile_policy(body.instruction))


@app.post("/api/policy/refine")
def refine_policy(body: PolicyRefineIn) -> dict:
    """Apply the customer's answers, then compile and show a fresh draft."""
    mandate = SESSION.mandate
    if mandate is None or mandate.status != "draft":
        raise HTTPException(409, "There is no draft policy to clarify.")

    questions = [PolicyQuestion(**question) for question in mandate.policy_questions]
    known = {question.id for question in questions}
    answers = {answer.question_id: answer.answer.strip() for answer in body.answers}
    unknown = sorted(set(answers) - known)
    if unknown:
        raise HTTPException(400, f"Unknown policy question(s): {', '.join(unknown)}")
    unanswered = [q.id for q in questions if q.blocking and not answers.get(q.id)]
    if unanswered:
        raise HTTPException(400, f"Answer required for: {', '.join(unanswered)}")

    revised, revision_activity = POLICY_CLARIFIER.revise_with_activity(
        mandate.instruction,
        questions,
        answers,
        use_ai=SESSION.settings.ai_enabled,
        failure_mode=SESSION.settings.ai_failure_mode,
    )
    if revised is None:
        raise HTTPException(
            503,
            "The policy clarification model is unavailable. Edit the instruction directly and draft it again.",
        )
    return _store_draft(compile_policy(revised), [revision_activity.to_payload()])


@app.post("/api/policy/confirm")
def confirm_policy(body: ConfirmIn) -> dict:
    """Record the customer's agreement. Only now does anything take effect."""
    if SESSION.mandate is None:
        raise HTTPException(409, "There is no draft to confirm.")
    if not body.confirmed:
        raise HTTPException(400, "A policy takes effect only when you confirm it.")
    blocking = [q for q in SESSION.mandate.policy_questions if q.get("blocking")]
    if blocking:
        raise HTTPException(409, "Answer the required policy question before confirming.")
    SESSION.mandate.status = "active"
    SESSION.mandate.mandate_id = SESSION.mandate.draft_id.replace("DRAFT", "TM")
    SESSION.mandate.confirmed_at = _now()
    STORE.save(SESSION)
    return {"mandate": asdict(SESSION.mandate)}


@app.get("/api/policy")
def read_policy() -> dict:
    if SESSION.mandate is None:
        return {"mandate": None}
    return {"mandate": asdict(SESSION.mandate)}


@app.post("/api/policy/tighten")
def tighten_policy(body: TightenIn) -> dict:
    """Tighten a live policy. It can never be loosened, only revoked."""
    mandate = _require_active()
    compiled = SESSION.compiled
    assert compiled is not None

    from ..models.enums import Operator, RuleScope
    from ..policy.compiler import CompiledRule

    additions: list[CompiledRule] = []
    for raw in body.add_rules:
        try:
            additions.append(
                CompiledRule(
                    field=raw["field"],
                    operator=Operator(raw["operator"]),
                    value=raw["value"],
                    currency=None,
                    scope=RuleScope(raw["scope"]) if raw.get("scope") else None,
                    period_days=raw.get("period_days"),
                    source="you tightened this",
                )
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, f"That rule is not usable: {exc}") from exc

    new_policy = None
    if body.uncertainty_policy:
        try:
            new_policy = UncertaintyPolicy(body.uncertainty_policy)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    review = review_amendment(
        compiled.hard_rules,
        compiled.hard_rules + additions,
        current_policy=UncertaintyPolicy(mandate.uncertainty_policy),
        new_policy=new_policy,
    )
    if not review.allowed:
        raise HTTPException(409, {"problems": review.problems, "notes": review.notes})

    compiled.hard_rules.extend(additions)
    mandate.hard_rules = [r.to_payload() for r in compiled.hard_rules]
    if new_policy:
        mandate.uncertainty_policy = new_policy.value
    mandate.amendments.append({"at": _now(), "notes": review.notes, "added": len(additions)})
    STORE.save(SESSION)
    return {"mandate": asdict(mandate), "notes": review.notes}


@app.delete("/api/policy")
def revoke_policy() -> dict:
    """Withdraw permission. The agent can spend nothing after this."""
    mandate = _require_active()
    mandate.status = "revoked"
    mandate.revoked_at = _now()
    STORE.save(SESSION)
    return {"mandate": asdict(mandate)}


# --- runs ------------------------------------------------------------------


@app.post("/api/runs/{scenario_id}")
def start_run(scenario_id: str) -> dict:
    """Replay a scenario against the confirmed policy."""
    mandate = _require_active()
    compiled = SESSION.compiled
    if compiled is None:
        raise HTTPException(409, "No compiled policy is in session.")
    if scenario_id not in BUILDER.catalogue:
        raise HTTPException(404, f"Unknown scenario {scenario_id}.")

    replay = BUILDER.build(scenario_id, compiled, mandate_id=mandate.mandate_id)
    state = RunState(run_id=scenario_id)
    engine = _demo_engine()
    steps: list[dict] = []

    for event in replay.events:
        verdict = engine.decide(event, state)
        record = DecisionRecord.build(scenario_id, event, verdict)
        SESSION.log.append(record)
        auth = event.authorization
        step = {
            **asdict(record),
            "merchant_country": auth.merchant.merchant_country,
            "merchant_category": auth.merchant.merchant_category,
            "currency": auth.currency.value,
            "amount": float(auth.amount),
            "items": [
                {
                    "line_no": i.line_no,
                    "name": i.item_name,
                    "category": i.item_category,
                    "quantity": i.quantity,
                    "unit_price": float(i.unit_price),
                    "details": i.item_details,
                }
                for i in auth.items
            ],
        }
        if verdict.clarification:
            clarification = verdict.clarification
            status = clarification.status
            if SESSION.settings.ai_failure_mode != "none":
                status = f"simulated_{SESSION.settings.ai_failure_mode}"
            elif not SESSION.settings.ai_enabled:
                status = "disabled"
            step["clarification"]["status"] = status
            step["ai_activity"] = [
                {
                    "model": Settings.from_env().llm_model,
                    "task": "Purchase clarification",
                    "result": (
                        "Reworded purchase question"
                        if not clarification.fallback_used
                        else "Used deterministic purchase question"
                    ),
                    "latency_ms": round(clarification.latency_ms, 1),
                    "fallback_used": clarification.fallback_used,
                    "status": status,
                }
            ]
        steps.append(step)
        if verdict.decision is Decision.STEP_UP:
            raised = datetime.now(timezone.utc)
            step["raised_at"] = raised.isoformat().replace("+00:00", "Z")
            step["expires_at"] = (
                raised + timedelta(seconds=HUMAN_TIMEOUT_SECONDS)
            ).isoformat().replace("+00:00", "Z")
            SESSION.pending[auth.authorization_id] = step

    SESSION.runs[scenario_id] = {
        "scenario_id": scenario_id,
        "name": replay.scenario_name,
        # What the customer actually authorised, which is what the purchases
        # were judged against. Showing the scenario's own wording here implied
        # a policy that was not in force.
        "instruction": mandate.instruction,
        # The scenario's nominal instruction, kept separate so a mismatch
        # between the two is visible rather than confusing.
        "scenario_instruction": replay.cardholder_instruction,
        "policy_matches_scenario": mandate.instruction.strip()
        == replay.cardholder_instruction.strip(),
        "steps": steps,
        "counts": _counts(steps),
        "state": state,
    }
    STORE.save(SESSION)
    return _run_payload(scenario_id)


def _counts(steps: list[dict]) -> dict[str, int]:
    tally = {d.value: 0 for d in Decision}
    for step in steps:
        tally[step["decision"]] += 1
    return tally


def _run_payload(scenario_id: str) -> dict:
    run = SESSION.runs[scenario_id]
    return {k: v for k, v in run.items() if k != "state"}


@app.get("/api/runs/{scenario_id}")
def read_run(scenario_id: str) -> dict:
    if scenario_id not in SESSION.runs:
        raise HTTPException(404, "That scenario has not been run in this session.")
    return _run_payload(scenario_id)


# --- the step-up inbox -----------------------------------------------------


def _sweep_lapsed() -> list[dict]:
    """Move purchases whose window closed out of the inbox.

    Nothing is sent anywhere. A decline submitted because nobody replied would
    be recorded as the customer's decision, which they never made. Lapsing is
    the absence of an answer, not an answer.
    """
    now = datetime.now(timezone.utc)
    moved = []
    for authorization_id, step in list(SESSION.pending.items()):
        expires = step.get("expires_at")
        if not expires:
            continue
        if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= now:
            step["lapsed"] = True
            SESSION.lapsed[authorization_id] = SESSION.pending.pop(authorization_id)
            moved.append(step)
    if moved:
        STORE.save(SESSION)
    return moved


@app.get("/api/pending")
def list_pending() -> dict:
    """Purchases paused for the customer, with the time they have left."""
    _sweep_lapsed()
    now = datetime.now(timezone.utc)
    pending = []
    for step in SESSION.pending.values():
        left = None
        if step.get("expires_at"):
            expires = datetime.fromisoformat(step["expires_at"].replace("Z", "+00:00"))
            left = max(0.0, (expires - now).total_seconds())
        pending.append({**step, "seconds_left": left})
    return {
        "pending": pending,
        "lapsed": list(SESSION.lapsed.values()),
        "human_timeout_seconds": HUMAN_TIMEOUT_SECONDS,
    }


@app.post("/api/pending/{authorization_id}/resolve")
def resolve_pending(authorization_id: str, body: ResolveIn) -> dict:
    """The customer's own answer. Only they can give it."""
    _sweep_lapsed()
    if authorization_id in SESSION.lapsed:
        raise HTTPException(
            410,
            "You were asked about this purchase and the window has closed. "
            "It was never approved, and nothing was answered on your behalf.",
        )
    if authorization_id not in SESSION.pending:
        raise HTTPException(404, "Nothing is waiting on you for that purchase.")
    try:
        decision = Decision(body.decision)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if decision is Decision.STEP_UP:
        raise HTTPException(400, "You can approve or decline. Pausing again is not an answer.")

    step = SESSION.pending.pop(authorization_id)
    step["resolved_by_customer"] = decision.value
    step["resolution_message"] = body.message

    for run in SESSION.runs.values():
        state: RunState = run["state"]
        if state.answered(authorization_id):
            state.resolve(authorization_id, decision)
        for existing in run["steps"]:
            if existing["authorization_id"] == authorization_id:
                existing["resolved_by_customer"] = decision.value
        run["counts"] = _counts(run["steps"])

    SESSION.log.note_resolution(authorization_id, decision)
    STORE.save(SESSION)
    return {"resolved": step, "remaining": len(SESSION.pending)}


@app.get("/api/journal")
def journal() -> dict:
    """Every decision this session made, with the evidence behind it."""
    return {"records": [asdict(r) for r in SESSION.log.records]}


@app.post("/api/session/reset")
def reset_session() -> dict:
    global SESSION
    settings = SESSION.settings
    SESSION = Session(settings=settings)
    STORE.clear()
    return {"reset": True}


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


__all__: list[str] = ["app", "serve"]
