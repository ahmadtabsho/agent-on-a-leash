"""Keep the test suite offline even when the developer's .env enables a model."""

import os

import pytest

# This runs while pytest loads conftest, before session-scoped engines and API
# module globals are constructed during test collection.
os.environ["LEASH_LLM_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def disable_environment_model(monkeypatch):
    monkeypatch.setenv("LEASH_LLM_ENABLED", "false")
