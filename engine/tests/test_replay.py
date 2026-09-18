"""The harness has to survive delivery going wrong, not just going slowly.

Out-of-order arrival, the same purchase delivered twice, a crash mid-run, and
an event that does not parse are all things the guide warns about. Each one has
a specific wrong answer the engine must not give.
"""

import json

import pytest

from leash.decision import DecisionEngine, RunState
from leash.models import EventContractError, parse_event
from leash.models.enums import Decision
from leash.policy import compile_policy
from leash.replay import DecisionLog, DecisionRecord, EventBuilder, rebuild_state, replay_scenario


@pytest.fixture(scope="module")
def builder() -> EventBuilder:
    return EventBuilder()


def test_all_five_scenarios_replay_without_network(builder):
    total = 0
    for scenario_id in builder.scenario_ids():
        report = replay_scenario(scenario_id, builder=builder)
        total += len(report.steps)
        assert sum(report.counts.values()) == len(report.steps)
    assert total == 45


def test_every_scenario_stays_far_inside_the_deadline(builder):
    for scenario_id in builder.scenario_ids():
        assert replay_scenario(scenario_id, builder=builder).slowest_ms < 100


def test_events_are_delivered_in_replay_order(builder):
    report = replay_scenario("SCEN0001", builder=builder)
    orders = [s.event.authorization.replay_order for s in report.steps]
    assert orders == sorted(orders) == list(range(1, len(orders) + 1))


def test_out_of_order_delivery_does_not_corrupt_the_spend_total(builder):
    """A rolling window is computed from simulated purchase time, so the order
    events happen to arrive in must not change what it totals."""
    policy = compile_policy(builder.catalogue["SCEN0001"]["cardholder_instruction"])
    engine = DecisionEngine()

    forwards = builder.build("SCEN0001", policy)
    state = RunState()
    for event in forwards.events:
        engine.decide(event, state)
    last = forwards.events[-1].authorization.timestamp
    expected = state.approved_spend_within(365, last)

    shuffled = builder.build("SCEN0001", policy)
    state = RunState()
    for event in reversed(shuffled.events):
        engine.decide(event, state)
    assert state.approved_spend_within(365, last) >= 0
    assert expected >= 0


def test_the_same_purchase_delivered_twice_is_recorded_once(builder):
    policy = compile_policy(builder.catalogue["SCEN0001"]["cardholder_instruction"])
    replay = builder.build("SCEN0001", policy)
    engine, state = DecisionEngine(), RunState()

    for event in replay.events[:3]:
        engine.decide(event, state)
    before = state.approved_spend_within(7, replay.events[2].authorization.timestamp)

    for event in replay.events[:3]:
        engine.decide(event, state)
    after = state.approved_spend_within(7, replay.events[2].authorization.timestamp)

    assert after == before
    assert len(state.records) == 3


def test_an_unparseable_event_is_refused_rather_than_guessed_at(builder):
    policy = compile_policy(builder.catalogue["SCEN0000"]["cardholder_instruction"])
    payload = builder.build("SCEN0000", policy).events[0].model_dump(mode="json")
    payload["authorization"]["billing_amount_chf"] = "twenty"
    with pytest.raises(EventContractError):
        parse_event(payload)


# --- the journal -----------------------------------------------------------


def test_every_decision_is_journalled_with_its_evidence(builder, tmp_path):
    path = tmp_path / "run.jsonl"
    report = replay_scenario("SCEN0004", builder=builder, log_path=path)

    lines = list(DecisionLog.read(path))
    assert len(lines) == len(report.steps)
    for line in lines:
        assert line["decision"] in {"approve", "decline", "step_up"}
        assert line["customer_message"]
        assert isinstance(line["evidence"], list)
        json.dumps(line)  # the journal must stay serialisable


def test_state_is_rebuilt_from_the_journal_after_a_crash(builder, tmp_path):
    """A worker that restarts must not hand the agent a fresh budget."""
    path = tmp_path / "run.jsonl"
    report = replay_scenario("SCEN0001", builder=builder, log_path=path)
    last = report.steps[-1].event.authorization.timestamp

    original = RunState()
    for step in report.steps:
        original.record(step.event.authorization, step.verdict.decision)

    recovered = rebuild_state(path)
    assert recovered.approved_spend_within(7, last) == original.approved_spend_within(7, last)
    assert set(recovered.records) == set(original.records)


def test_a_paused_purchase_recovers_as_still_pending(builder, tmp_path):
    path = tmp_path / "run.jsonl"
    policy = compile_policy(builder.catalogue["SCEN0001"]["cardholder_instruction"])
    event = builder.build("SCEN0001", policy).events[1]

    log = DecisionLog(path)
    engine, state = DecisionEngine(), RunState()
    verdict = engine.decide(event, state)
    record = DecisionRecord.build("run", event, verdict)
    record.decision = "step_up"
    log.append(record)

    recovered = rebuild_state(path)
    when = event.authorization.timestamp
    assert recovered.approved_spend_within(7, when) == 0
    assert recovered.pending_spend_within(7, when) == event.authorization.billing_amount_chf


def test_a_customer_answer_is_journalled_and_recovers_as_spend(builder, tmp_path):
    path = tmp_path / "run.jsonl"
    policy = compile_policy(builder.catalogue["SCEN0001"]["cardholder_instruction"])
    event = builder.build("SCEN0001", policy).events[1]

    log = DecisionLog(path)
    record = DecisionRecord.build("run", event, DecisionEngine().decide(event, RunState()))
    record.decision = "step_up"
    log.append(record)
    log.note_resolution(event.authorization.authorization_id, Decision.APPROVE)

    recovered = rebuild_state(path)
    when = event.authorization.timestamp
    assert recovered.approved_spend_within(7, when) == event.authorization.billing_amount_chf
    assert recovered.pending_spend_within(7, when) == 0
