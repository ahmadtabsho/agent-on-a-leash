"""Developer entry point: `leash <command>`."""

from __future__ import annotations

import argparse
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


COMMANDS = {"check": cmd_check, "health": cmd_health}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="leash", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in COMMANDS.items():
        sub.add_parser(name, help=(fn.__doc__ or "").strip().splitlines()[0])
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
