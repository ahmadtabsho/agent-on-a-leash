"""The control session has to survive a restart.

The decision journal already did. The mandate and the step-up inbox did not,
which is the worse half to lose: a restart left the customer with no policy and
no record that anything was waiting on them, while the decisions already sent
stayed on the platform.
"""

import json

import pytest
from fastapi.testclient import TestClient

from leash.api import control
from leash.api.store import SessionStore
from leash.models.enums import Decision

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
def store(tmp_path, monkeypatch) -> SessionStore:
    """Point the control API at a throwaway session file."""
    store = SessionStore(tmp_path / "session.json")
    monkeypatch.setattr(control, "STORE", store)
    return store


@pytest.fixture
def client(store) -> TestClient:
    c = TestClient(control.app)
    c.post("/api/session/reset")
    yield c
    c.post("/api/session/reset")


def restart(store) -> None:
    """Simulate the process dying and coming back."""
    control.SESSION = control._restore()


def authorise(client, instruction: str) -> dict:
    client.post("/api/policy/draft", json={"instruction": instruction})
    return client.post("/api/policy/confirm", json={"confirmed": True}).json()["mandate"]


# --- the file -------------------------------------------------------------


def test_nothing_is_written_until_something_happens(store, client):
    assert not store.path.exists()


def test_confirming_a_policy_writes_it_to_disk(store, client):
    authorise(client, GROCERIES)
    assert store.path.exists()
    payload = json.loads(store.path.read_text())
    assert payload["mandate"]["status"] == "active"
    assert payload["instruction"] == GROCERIES


def test_the_write_is_valid_json_after_every_change(store, client):
    authorise(client, GROCERIES)
    client.post("/api/runs/SCEN0001")
    json.loads(store.path.read_text())  # raises if a write was left half-finished


# --- restoring ------------------------------------------------------------


def test_the_policy_survives_a_restart(store, client):
    original = authorise(client, GROCERIES)
    restart(store)

    mandate = client.get("/api/policy").json()["mandate"]
    assert mandate["status"] == "active"
    assert mandate["mandate_id"] == original["mandate_id"]
    assert mandate["instruction"] == GROCERIES


def test_the_agent_can_keep_shopping_after_a_restart(store, client):
    """The compiled policy has to come back too, not just its description."""
    authorise(client, GROCERIES)
    restart(store)

    body = client.post("/api/runs/SCEN0001")
    assert body.status_code == 200
    assert sum(body.json()["counts"].values()) == 10


def test_the_step_up_inbox_survives_a_restart(store, client):
    authorise(client, MONITOR)
    client.post("/api/runs/SCEN0004")
    waiting = {p["authorization_id"] for p in client.get("/api/pending").json()["pending"]}
    assert waiting

    restart(store)

    after = {p["authorization_id"] for p in client.get("/api/pending").json()["pending"]}
    assert after == waiting, "the customer must not lose what was asked of them"


def test_a_purchase_can_be_answered_after_a_restart(store, client):
    authorise(client, MONITOR)
    client.post("/api/runs/SCEN0004")
    target = client.get("/api/pending").json()["pending"][0]["authorization_id"]

    restart(store)

    response = client.post(f"/api/pending/{target}/resolve", json={"decision": "approve"})
    assert response.status_code == 200
    assert response.json()["resolved"]["resolved_by_customer"] == "approve"


def test_the_journal_survives_a_restart(store, client):
    authorise(client, GROCERIES)
    client.post("/api/runs/SCEN0001")
    before = client.get("/api/journal").json()["records"]

    restart(store)

    after = client.get("/api/journal").json()["records"]
    assert len(after) == len(before) == 10
    assert [r["authorization_id"] for r in after] == [r["authorization_id"] for r in before]


def test_spending_already_approved_still_counts_after_a_restart(store, client):
    """The point of persisting. A restart that forgot the spend would hand the
    agent its budget a second time."""
    authorise(client, GROCERIES)
    client.post("/api/runs/SCEN0001")

    restart(store)

    run = control.SESSION.runs["SCEN0001"]
    state = run["state"]
    approved = [r for r in state.records.values() if r.decision is Decision.APPROVE]
    assert approved, "approvals must come back as approvals"
    assert sum(r.billing_amount_chf for r in approved) > 0


def test_revocation_survives_a_restart(store, client):
    """A withdrawn permission must not come back granted."""
    authorise(client, GROCERIES)
    client.delete("/api/policy")

    restart(store)

    assert client.get("/api/policy").json()["mandate"]["status"] == "revoked"
    assert client.post("/api/runs/SCEN0001").status_code == 409


def test_health_reports_whether_a_session_was_restored(store, client):
    assert client.get("/api/health").json()["session_restored"] is False
    authorise(client, GROCERIES)
    restart(store)
    assert client.get("/api/health").json()["session_restored"] is True


# --- refusing to half-restore ---------------------------------------------


def test_a_reset_clears_the_file_too(store, client):
    authorise(client, GROCERIES)
    assert store.path.exists()

    client.post("/api/session/reset")

    assert not store.path.exists()
    restart(store)
    assert client.get("/api/policy").json()["mandate"] is None


def test_a_corrupt_file_starts_clean_rather_than_half_read(store, client):
    authorise(client, GROCERIES)
    store.path.write_text("{ this is not json")

    restart(store)

    assert client.get("/api/policy").json()["mandate"] is None


def test_an_older_schema_is_discarded(store, client):
    """Restoring a policy we cannot fully understand is worse than asking for
    it again."""
    authorise(client, GROCERIES)
    payload = json.loads(store.path.read_text())
    payload["schema_version"] = 0
    store.path.write_text(json.dumps(payload))

    restart(store)

    assert client.get("/api/policy").json()["mandate"] is None


def test_an_unreadable_location_does_not_break_a_live_request(tmp_path, monkeypatch):
    """Losing the ability to restore is bad; refusing to answer is worse."""
    blocked = SessionStore(tmp_path / "nope" / "session.json")
    monkeypatch.setattr(blocked, "path", tmp_path / "nope")  # a directory, not a file
    monkeypatch.setattr(control, "STORE", blocked)
    (tmp_path / "nope").mkdir()

    c = TestClient(control.app)
    c.post("/api/session/reset")
    response = c.post("/api/policy/draft", json={"instruction": GROCERIES})
    assert response.status_code == 200
