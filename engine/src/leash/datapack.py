"""Loader for the vendored synthetic data pack.

The pack ships a manifest with row counts and hashes. We check against it on
startup so a truncated or edited CSV fails loudly here rather than silently
changing a decision later.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR


@dataclass(frozen=True)
class PackCheck:
    name: str
    expected_rows: int | None
    actual_rows: int | None
    sha256: str | None
    ok: bool
    problem: str = ""


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_table(name: str, data_dir: Path = DATA_DIR) -> list[dict[str, str]]:
    """Read one CSV of the pack as a list of row dicts."""
    return _read_csv(data_dir / f"{name}.csv")


def load_metadata(data_dir: Path = DATA_DIR) -> dict:
    with (data_dir / "metadata.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def _manifest_entries(metadata: dict) -> list[dict]:
    """Normalise the manifest's file list to dicts with path/rows/sha256."""
    files = metadata.get("files")
    if isinstance(files, list):
        return [f for f in files if isinstance(f, dict) and f.get("path")]
    if isinstance(files, dict):
        return [
            {"path": key, **value}
            for key, value in files.items()
            if isinstance(value, dict)
        ]
    return []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_pack(data_dir: Path = DATA_DIR) -> list[PackCheck]:
    """Verify every manifest entry against the file on disk.

    A mismatch means the pack was edited or truncated. We want that to surface
    here, not as a quietly different decision three stages later.
    """
    results: list[PackCheck] = []
    for entry in _manifest_entries(load_metadata(data_dir)):
        rel = str(entry["path"])
        path = data_dir / rel
        if not path.exists():
            results.append(PackCheck(rel, entry.get("rows"), 0, None, False, "missing"))
            continue

        rows = len(_read_csv(path)) if path.suffix == ".csv" else None
        want_rows = entry.get("rows")
        want_hash = entry.get("sha256")
        digest = _sha256(path)

        problems = []
        if want_rows is not None and rows != want_rows:
            problems.append(f"rows {rows} != {want_rows}")
        if want_hash and digest != want_hash:
            problems.append("sha256 mismatch")

        results.append(
            PackCheck(rel, want_rows, rows, digest, not problems, "; ".join(problems))
        )
    return results
