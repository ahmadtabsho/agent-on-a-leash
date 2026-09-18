"""The whole live sequence, in one command.

On the event day the flow is: compile a policy, store it as a draft, have the
customer confirm it, start a run, then answer every purchase before its
deadline. Doing the first four by hand under time pressure is how a demo goes
wrong, so they are scripted here.

Two details of the ordering matter and are easy to get backwards:

* **The worker is ready before the run starts.** The decision deadline is eight
  seconds from when a request is *queued*, not from when we poll for it, so a
  run started before the poller is listening has already spent part of its
  budget.
* **Confirmation is a real gate.** The draft is shown and nothing runs until it
  is accepted. `--yes` exists for scripted use; it stands in for the customer
  saying yes, it does not skip the step.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

from ..api.client import ApiError, LeashClient
from ..decision import DecisionEngine, RunState
from ..models.enums import Decision
from ..policy import CompiledPolicy, compile_policy
from ..replay.builder import EventBuilder
from .loop import Worker, WorkerStats


@dataclass
class LiveRun:
    """What one live run produced."""

    scenario_id: str
    instruction: str
    policy: CompiledPolicy
    draft_id: str | None = None
    mandate_id: str | None = None
    run_id: str | None = None
    stats: WorkerStats | None = None
    pending: list = dc_field(default_factory=list)
    lapsed: list = dc_field(default_factory=list)
    human_timeout_seconds: float | None = None
    progress: dict = dc_field(default_factory=dict)
    steps: list[tuple[str, str]] = dc_field(default_factory=list)

    def note(self, stage: str, detail: str) -> None:
        self.steps.append((stage, detail))


def _scenario_instruction(scenario_id: str) -> str:
    catalogue = EventBuilder().catalogue
    if scenario_id not in catalogue:
        raise KeyError(f"unknown scenario {scenario_id!r}; have {', '.join(sorted(catalogue))}")
    return catalogue[scenario_id]["cardholder_instruction"]


def prepare(scenario_id: str, instruction: str | None = None) -> LiveRun:
    """Compile the policy. No network, so this can be reviewed before anything runs."""
    text = instruction or _scenario_instruction(scenario_id)
    return LiveRun(scenario_id=scenario_id, instruction=text, policy=compile_policy(text))


def execute(
    live: LiveRun,
    client: LeashClient,
    *,
    engine: DecisionEngine | None = None,
    log_path: Path | None = None,
    wait: int = 25,
    max_empty_polls: int = 3,
) -> LiveRun:
    """Store the confirmed policy, start the run, and answer every purchase.

    Assumes the customer has already agreed to `live.policy`; the caller owns
    that gate.
    """
    engine = engine or DecisionEngine()

    draft = client.create_mandate(live.policy.to_draft_payload())
    live.draft_id = draft.get("draft_id") or draft.get("data", {}).get("draft_id")
    if not live.draft_id:
        raise ApiError(None, f"no draft_id in the mandate response: {draft}")
    live.note("draft", f"stored {len(live.policy.hard_rules)} rule(s) as {live.draft_id}")

    confirmed = client.confirm_mandate(live.draft_id)
    live.mandate_id = confirmed.get("mandate_id") or confirmed.get("data", {}).get("mandate_id")
    if not live.mandate_id:
        raise ApiError(None, f"no mandate_id in the confirmation response: {confirmed}")
    live.note("confirm", f"the customer's agreement is recorded as {live.mandate_id}")

    # The worker exists before the run does, so nothing is queued while we are
    # still setting up.
    worker = Worker(client, engine, RunState(), log_path=log_path)
    live.human_timeout_seconds = worker.adopt_timeouts()
    window = (
        f"; the customer has {live.human_timeout_seconds:.0f}s to answer anything we pause"
        if live.human_timeout_seconds
        else ""
    )
    live.note("worker", f"ready to receive{window}")

    started = client.start_run(live.scenario_id, live.mandate_id)
    live.run_id = started.get("run_id") or started.get("data", {}).get("run_id")
    live.note("run", f"{live.scenario_id} started as {live.run_id}")

    live.stats = worker.run_until_idle(wait=wait, max_empty_polls=max_empty_polls)
    worker.sweep_expired()
    live.pending = list(worker.pending.values())
    live.lapsed = list(worker.lapsed.values())

    if live.run_id:
        try:
            live.progress = client.run_progress(live.run_id)
        except ApiError:
            # Progress is reporting, not correctness. A failure here must not
            # discard the decisions we already sent.
            live.progress = {}

    live._worker = worker
    return live


def resolve_pending(live: LiveRun, authorization_id: str, decision: Decision, message: str = "") -> dict:
    """Send the customer's own answer for a purchase that was paused."""
    worker: Worker = getattr(live, "_worker", None)
    if worker is None:
        raise RuntimeError("no worker on this run; call execute() first")
    worker.sweep_expired()
    response = worker.resolve(authorization_id, decision, message)
    live.pending = list(worker.pending.values())
    live.lapsed = list(worker.lapsed.values())
    return response
