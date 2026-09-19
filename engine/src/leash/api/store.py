"""Somewhere for the control session to live across a restart.

The decision journal already survives a crash. The mandate and the step-up
inbox did not, which is the worse half to lose: a restart left the customer
with no policy and no record that anything was waiting on them, while the
purchases those decisions had already been sent for stayed on the platform.

This is a small JSON file, written whole on each change. Not a database, and
deliberately so — a prototype that needs a running Postgres to demo is a
prototype nobody sees. The write is atomic (write a temp file, then rename) so
an interrupted save cannot leave a half-written session behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "runs" / "control-session.json"
SCHEMA_VERSION = 1


class SessionStore:
    """Reads and writes one control session as a single JSON document."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_PATH

    # --- writing -----------------------------------------------------------

    def save(self, session: Any) -> None:
        """Persist the session.

        A failure here must never take down a live request — losing the ability
        to restore is bad, refusing to answer the customer is worse.
        """
        try:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "mandate": asdict(session.mandate) if session.mandate else None,
                "instruction": session.compiled.instruction if session.compiled else None,
                "pending": session.pending,
                "lapsed": session.lapsed,
                "settings": asdict(session.settings),
                "runs": {
                    scenario_id: {k: v for k, v in run.items() if k != "state"}
                    for scenario_id, run in session.runs.items()
                },
                "journal": [asdict(record) for record in session.log.records],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(temp_name, self.path)
        except (OSError, TypeError, ValueError):
            # Persistence is a convenience. The session in memory is still correct.
            return

    # --- reading -----------------------------------------------------------

    def load(self) -> dict | None:
        """Read a saved session, or None if there is nothing usable."""
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            # An older shape is discarded rather than half-read. Restoring a
            # policy we cannot fully understand is worse than asking for it again.
            return None
        return payload

    def clear(self) -> None:
        """Forget the saved session.

        Defensive for the same reason `save` is: a store that cannot be written
        must not stop the customer using the system.
        """
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return
