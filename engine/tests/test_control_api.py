"""The customer's control surface.

What matters here is not the HTTP plumbing but the order of authority: nothing
takes effect before the customer confirms it, a live policy can only be
tightened, revoking stops everything, and only the customer answers a purchase
that was paused for them.
"""

import pytest
from fastapi.testclient import TestClient

from leash.api import control
from leash.config import Settings
from leash.llm import PolicyClarifier

GROCERIES = (
    "Order our household groceries for delivery. Keep each order at or below CHF 120 "
    "including delivery, and keep the total across any seven days at or below CHF 300. "
    "Ask me when uncertain."
)
MONITOR = (
    "Buy the 27-inch monitor I chose, from a seller I have bought from before, for "
    "CHF 400 or less. Do not add anything I did not ask for. Ask me when uncertain."
)


@pytest.fixture
def client() -> TestClient:
    c = TestClient(control.app)
    c.post("/api/session/reset")
    c.put(
        "/api/settings",
        json={"ai_enabled": True, "show_ai_activity": True, "ai_failure_mode": "none"},
    )
    yield c
    c.post("/api/session/reset")


def authorise(client, instruction: str) -> dict:
    client.post("/api/policy/draft", json={"instruction": instruction})
    return client.post("/api/policy/confirm", json={"confirmed": True}).json()["mandate"]


def test_health_reports_the_mode_and_the_supplied_scenarios(client):
    body = client.get("/api/health").json()
    assert body["mode"] in {"offline", "sandbox"}
    assert len(body["scenarios"]) == 5
    assert body["engine_version"].startswith("leash/")


def test_demo_settings_can_disable_ai_details_and_simulate_failure(client):
    updated = client.put(
        "/api/settings",
        json={
            "ai_enabled": False,
            "show_ai_activity": False,
            "ai_failure_mode": "timeout",
        },
    )
    assert updated.status_code == 200
    assert client.get("/api/settings").json() == updated.json()


def test_a_preview_authorises_nothing(client):
    body = client.post("/api/policy/preview", json={"instruction": GROCERIES}).json()
    assert body["hard_rules"]
    assert client.get("/api/policy").json()["mandate"] is None


def test_a_draft_shows_the_checks_and_the_open_questions(client):
    body = client.post("/api/policy/draft", json={"instruction": MONITOR}).json()
    assert body["mandate"]["status"] == "draft"
    assert body["policy"]["hard_rules"]
    assert body["policy"]["open_questions"]
    assert body["policy"]["policy_questions"]
    assert all("source" in r for r in body["policy"]["hard_rules"])


def test_a_blocking_policy_ambiguity_must_be_answered_and_recompiled(client, monkeypatch):
    def completion(system, user, *, timeout_s):
        if "Rewrite one policy clarification" in system:
            return '{"question":"You gave incompatible amount directions. Which limit did you mean?"}'
        return '{"instruction":"Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. Ask me when uncertain."}'

    settings = Settings("https://sandbox.invalid", None, 2500, True, "test-model", 1200)
    monkeypatch.setattr(control, "POLICY_CLARIFIER", PolicyClarifier(settings, completion))
    instruction = (
        "Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. "
        "Ask me when uncertain. Buy an item for CHF 30 or more."
    )
    draft = client.post("/api/policy/draft", json={"instruction": instruction}).json()
    blocking = [q for q in draft["policy"]["policy_questions"] if q["blocking"]]
    assert len(blocking) == 1
    assert blocking[0]["generated_by"] == "test-model"
    assert draft["policy"]["ai_activity"][0]["fallback_used"] is False
    assert client.post("/api/policy/confirm", json={"confirmed": True}).status_code == 409

    refined = client.post(
        "/api/policy/refine",
        json={
            "answers": [
                {
                    "question_id": blocking[0]["id"],
                    "answer": "The CHF 30 sentence was a mistake. Keep the CHF 20 maximum.",
                }
            ]
        },
    )
    assert refined.status_code == 200
    policy = refined.json()["policy"]
    assert not [q for q in policy["policy_questions"] if q["blocking"]]
    assert any(rule["value"] == 20.0 for rule in policy["hard_rules"])
    assert client.post("/api/policy/confirm", json={"confirmed": True}).status_code == 200


def test_failure_demo_uses_code_question_and_keeps_confirmation_blocked(client):
    client.put(
        "/api/settings",
        json={"ai_enabled": True, "show_ai_activity": True, "ai_failure_mode": "timeout"},
    )
    draft = client.post(
        "/api/policy/draft",
        json={"instruction": "Buy only groceries under CHF 20. Buy electronics under CHF 30."},
    ).json()
    conflicts = [q for q in draft["policy"]["policy_questions"] if q["blocking"]]
    assert conflicts and conflicts[0]["generated_by"] == "code"
    activity = draft["policy"]["ai_activity"][0]
    assert activity["fallback_used"] is True
    assert activity["status"] == "simulated_timeout"
    assert client.post("/api/policy/confirm", json={"confirmed": True}).status_code == 409


def test_runtime_failure_demo_still_pauses_with_a_safe_code_question(client):
    authorise(client, MONITOR)
    client.put(
        "/api/settings",
        json={"ai_enabled": True, "show_ai_activity": True, "ai_failure_mode": "timeout"},
    )

    body = client.post("/api/runs/SCEN0004").json()
    paused = [step for step in body["steps"] if step["decision"] == "step_up"]

    assert paused
    assert all(step["clarification"]["generated_by"] == "code" for step in paused)
    assert all(step["clarification"]["fallback_used"] is True for step in paused)
    assert all(step["clarification"]["status"] == "simulated_timeout" for step in paused)
    assert all(step["clarification"]["question"].endswith("?") for step in paused)


def test_nothing_can_be_spent_before_the_customer_confirms(client):
    client.post("/api/policy/draft", json={"instruction": GROCERIES})
    assert client.post("/api/runs/SCEN0001").status_code == 409


def test_confirming_activates_the_policy(client):
    mandate = authorise(client, GROCERIES)
    assert mandate["status"] == "active"
    assert mandate["mandate_id"] and mandate["confirmed_at"]


def test_refusing_to_confirm_leaves_it_a_draft(client):
    client.post("/api/policy/draft", json={"instruction": GROCERIES})
    assert client.post("/api/policy/confirm", json={"confirmed": False}).status_code == 400


def test_a_run_returns_every_decision_with_its_evidence(client):
    authorise(client, GROCERIES)
    body = client.post("/api/runs/SCEN0001").json()
    assert len(body["steps"]) == 10
    assert sum(body["counts"].values()) == 10
    for step in body["steps"]:
        assert step["customer_message"]
        assert isinstance(step["evidence"], list)
        assert step["items"]


def test_a_paused_purchase_appears_in_the_inbox(client):
    authorise(client, MONITOR)
    client.post("/api/runs/SCEN0004")
    pending = client.get("/api/pending").json()["pending"]
    assert pending
    assert all(p["decision"] == "step_up" for p in pending)
    assert all(p["clarification"]["question"].endswith("?") for p in pending)
    assert all(
        [choice["decision"] for choice in p["clarification"]["choices"]]
        == ["approve", "decline"]
        for p in pending
    )


def test_only_the_customer_answers_a_paused_purchase(client):
    authorise(client, MONITOR)
    client.post("/api/runs/SCEN0004")
    pending = client.get("/api/pending").json()["pending"]
    target = pending[0]["authorization_id"]

    body = client.post(f"/api/pending/{target}/resolve", json={"decision": "approve"}).json()
    assert body["resolved"]["resolved_by_customer"] == "approve"
    still_waiting = {p["authorization_id"] for p in client.get("/api/pending").json()["pending"]}
    assert target not in still_waiting


def test_pausing_again_is_not_an_answer(client):
    authorise(client, MONITOR)
    client.post("/api/runs/SCEN0004")
    target = client.get("/api/pending").json()["pending"][0]["authorization_id"]
    response = client.post(f"/api/pending/{target}/resolve", json={"decision": "step_up"})
    assert response.status_code == 400


def test_answering_an_unknown_purchase_is_refused(client):
    authorise(client, MONITOR)
    assert client.post("/api/pending/NOPE/resolve", json={"decision": "approve"}).status_code == 404


def test_a_live_policy_can_be_tightened(client):
    authorise(client, GROCERIES)
    body = client.post("/api/policy/tighten", json={"uncertainty_policy": "decline"}).json()
    assert body["mandate"]["uncertainty_policy"] == "decline"
    assert body["mandate"]["amendments"]


def test_a_live_policy_cannot_be_loosened(client):
    authorise(client, GROCERIES)
    client.post("/api/policy/tighten", json={"uncertainty_policy": "decline"})
    response = client.post("/api/policy/tighten", json={"uncertainty_policy": "ask"})
    assert response.status_code == 409


def test_adding_a_contradictory_rule_is_refused(client):
    authorise(client, GROCERIES)
    response = client.post(
        "/api/policy/tighten",
        json={
            "add_rules": [
                {
                    "field": "authorization.billing_amount_chf",
                    "operator": ">=",
                    "value": 500,
                    "scope": "purchase",
                }
            ]
        },
    )
    assert response.status_code == 409


def test_revoking_stops_everything(client):
    authorise(client, GROCERIES)
    assert client.delete("/api/policy").json()["mandate"]["status"] == "revoked"
    assert client.post("/api/runs/SCEN0001").status_code == 409
    tighten = client.post("/api/policy/tighten", json={"uncertainty_policy": "decline"})
    assert tighten.status_code == 409


def test_the_journal_records_every_decision(client):
    authorise(client, GROCERIES)
    client.post("/api/runs/SCEN0001")
    records = client.get("/api/journal").json()["records"]
    assert len(records) == 10
    assert all(r["reason_codes"] is not None for r in records)
