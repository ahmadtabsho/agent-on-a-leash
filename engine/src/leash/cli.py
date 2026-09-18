"""Developer entry point: `leash <command>`."""

from __future__ import annotations

import argparse
import json
import sys

import httpx
from dotenv import load_dotenv

from .config import Settings
from .datapack import check_pack


def cmd_check(_: argparse.Namespace) -> int:
    """Verify the vendored data pack against its manifest."""
    results = check_pack()
    failures = [r for r in results if not r.ok]
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        rows = f"{r.actual_rows} rows" if r.actual_rows is not None else "-"
        note = f"  <- {r.problem}" if r.problem else ""
        print(f"  [{mark}] {r.name:<52} {rows:>10}{note}")
    print(f"\n{len(results) - len(failures)}/{len(results)} files verified.")
    return 1 if failures else 0


def cmd_health(_: argparse.Namespace) -> int:
    """Probe the hosted sandbox. Needs no API key."""
    settings = Settings.from_env()
    url = f"{settings.base_url}/healthz"
    try:
        response = httpx.get(url, timeout=15)
    except httpx.HTTPError as exc:
        print(f"unreachable: {url}\n  {exc}")
        return 1
    print(f"{response.status_code} {url}\n  {response.text.strip()}")
    if settings.api_key:
        print("  TEAM_API_KEY is set.")
    else:
        print("  TEAM_API_KEY is not set; keyed endpoints stay unavailable.")
    return 0 if response.status_code == 200 else 1


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate JSON files against the authorization event contract."""
    from .models import EventContractError, parse_event

    failed = 0
    for path in args.paths:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  [FAIL] {path}\n           not readable JSON: {exc}")
            failed += 1
            continue
        # Accept either a bare event or a poll envelope wrapping one.
        payload = raw.get("data", raw) if isinstance(raw, dict) else raw
        try:
            event = parse_event(payload)
        except EventContractError as exc:
            print(f"  [FAIL] {path}  ({exc.stage})")
            for problem in exc.problems:
                print(f"           {problem}")
            failed += 1
            continue
        auth = event.authorization
        print(f"  [ok  ] {path}  {auth.authorization_id}  {auth.billing_amount_chf} CHF")
    print(f"\n{len(args.paths) - failed}/{len(args.paths)} events valid.")
    return 1 if failed else 0


COMMANDS = {"check": cmd_check, "health": cmd_health, "validate": cmd_validate}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="leash", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in COMMANDS.items():
        parser_ = sub.add_parser(name, help=(fn.__doc__ or "").strip().splitlines()[0])
        if name == "validate":
            parser_.add_argument("paths", nargs="+", help="JSON event or envelope files")
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
