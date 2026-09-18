"""Parse an incoming event against the published contract, then our own types.

Two gates, deliberately:

1. `authorization_event.schema.json` as shipped by the platform. It is the
   authoritative contract, so we check against the file itself rather than
   against our reading of it.
2. The Pydantic models, which add the semantics the schema cannot express —
   Decimal money, aware timestamps, and the derived properties the engine uses.

An event that fails either gate is never guessed at. The caller escalates.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ..config import SCHEMA_DIR
from .events import AuthorizationEvent, Envelope


class EventContractError(ValueError):
    """An event did not match the published contract or our types.

    Carries every problem found, not just the first, so a malformed offline
    event can be fixed in one pass.
    """

    def __init__(self, stage: str, problems: list[str]):
        self.stage = stage
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"{stage} validation failed:\n  - {joined}")


@lru_cache(maxsize=1)
def event_validator(schema_dir: Path = SCHEMA_DIR) -> Draft202012Validator:
    with (schema_dir / "authorization_event.schema.json").open(encoding="utf-8") as fh:
        schema = json.load(fh)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_problems(raw: dict[str, Any]) -> list[str]:
    """Every way `raw` departs from the published event schema."""
    problems = []
    for error in sorted(event_validator().iter_errors(raw), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in error.path) or "<root>"
        problems.append(f"{where}: {error.message}")
    return problems


def _model_problems(exc: ValidationError) -> list[str]:
    return [
        "{}: {}".format("/".join(str(p) for p in err["loc"]) or "<root>", err["msg"])
        for err in exc.errors()
    ]


def parse_event(raw: dict[str, Any]) -> AuthorizationEvent:
    """Validate and parse one authorization request.

    Raises `EventContractError` rather than returning a partially understood
    event. Deciding on a message we could not fully read is exactly the failure
    mode this layer exists to prevent.
    """
    problems = schema_problems(raw)
    if problems:
        raise EventContractError("schema", problems)
    try:
        return AuthorizationEvent.model_validate(raw)
    except ValidationError as exc:
        raise EventContractError("model", _model_problems(exc)) from exc


def parse_envelope(raw: dict[str, Any]) -> Envelope:
    """Validate a poll response: the envelope's `data` must be a valid event."""
    if not isinstance(raw, dict) or "data" not in raw:
        raise EventContractError("envelope", ["<root>: missing 'data'"])
    parse_event(raw["data"])
    try:
        return Envelope.model_validate(raw)
    except ValidationError as exc:
        raise EventContractError("envelope", _model_problems(exc)) from exc
