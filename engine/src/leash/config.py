"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_BASE_URL = (
    "https://saw26api.ashyground-364e1d07.switzerlandnorth.azurecontainerapps.io"
)

# The repo root holds the vendored data pack; the engine lives one level down.
REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
SCHEMA_DIR = DATA_DIR / "schemas"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str | None
    decision_budget_ms: int
    llm_enabled: bool
    llm_model: str
    llm_timeout_ms: int

    @classmethod
    def from_env(cls) -> Settings:
        # Load .env here rather than only at the CLI entry point. The control
        # API, the worker and any script all read settings through this, and a
        # key that loads for one caller but not another is worse than no key.
        load_dotenv(REPO_ROOT / ".env")
        return cls(
            base_url=os.environ.get("LEASH_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_key=os.environ.get("TEAM_API_KEY") or None,
            decision_budget_ms=_int_env("LEASH_DECISION_BUDGET_MS", 2500),
            llm_enabled=os.environ.get("LEASH_LLM_ENABLED", "").lower()
            in {"1", "true", "yes"},
            llm_model=os.environ.get("LEASH_LLM_MODEL", "claude-haiku-4-5-20251001"),
            llm_timeout_ms=_int_env("LEASH_LLM_TIMEOUT_MS", 900),
        )
