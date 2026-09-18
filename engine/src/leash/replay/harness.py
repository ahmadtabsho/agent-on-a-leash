"""Replay a scenario offline and report what the engine did and why."""

from __future__ import annotations

from dataclasses import dataclass

from ..decision import DecisionEngine, RunState, Verdict
from ..models.enums import Decision
from ..models.events import AuthorizationEvent
from ..policy import CompiledPolicy, compile_policy
from .builder import EventBuilder, ScenarioReplay


@dataclass
class Step:
    event: AuthorizationEvent
    verdict: Verdict


@dataclass
class RunReport:
    replay: ScenarioReplay
    policy: CompiledPolicy
    steps: list[Step]

    @property
    def counts(self) -> dict[str, int]:
        tally = {d.value: 0 for d in Decision}
        for step in self.steps:
            tally[step.verdict.decision.value] += 1
        return tally

    @property
    def slowest_ms(self) -> float:
        return max((s.verdict.elapsed_ms for s in self.steps), default=0.0)


def replay_scenario(
    scenario_id: str,
    *,
    builder: EventBuilder | None = None,
    engine: DecisionEngine | None = None,
) -> RunReport:
    builder = builder or EventBuilder()
    engine = engine or DecisionEngine()
    policy = compile_policy(builder.catalogue[scenario_id]["cardholder_instruction"])
    replay = builder.build(scenario_id, policy)
    state = RunState(run_id=scenario_id)
    steps = [Step(event, engine.decide(event, state)) for event in replay.events]
    return RunReport(replay=replay, policy=policy, steps=steps)
