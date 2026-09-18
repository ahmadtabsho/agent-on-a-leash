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


def cmd_policy(args: argparse.Namespace) -> int:
    """Compile an instruction into checks, guidance, and open questions."""
    from .datapack import load_table
    from .policy import compile_policy

    text = args.instruction
    if not text:
        catalogue = {r["scenario_id"]: r["cardholder_instruction"] for r in load_table("scenario_catalogue")}
        if args.scenario not in catalogue:
            print(f"unknown scenario {args.scenario!r}; have {', '.join(sorted(catalogue))}")
            return 1
        text = catalogue[args.scenario]

    policy = compile_policy(text)
    if args.json:
        print(json.dumps(policy.to_draft_payload(), indent=2))
        return 0

    print(f'\nInstruction\n  "{text}"\n')
    print("Checks we will enforce")
    for rule in policy.hard_rules:
        window = f" over {rule.period_days}d" if rule.period_days else ""
        value = rule.value if not isinstance(rule.value, list) else ", ".join(rule.value)
        print(f"  - {rule.field} {rule.operator.value} {value}{window}")
    print("\nWhat that means")
    for line in policy.guidance:
        print(f"  - {line}")
    if policy.open_questions:
        print("\nWe need you to decide")
        for question in policy.open_questions:
            print(f"  ? {question}")
    print(f"\nWhen we cannot settle a purchase: {policy.uncertainty_policy.value}\n")
    return 0


MARK = {"approve": "approve ", "decline": "DECLINE ", "step_up": "ASK      "}


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay scenarios offline and show every decision with its reasons."""
    from .replay import EventBuilder, replay_scenario

    builder = EventBuilder()
    targets = [args.scenario] if args.scenario else builder.scenario_ids()
    slowest = 0.0

    for scenario_id in targets:
        report = replay_scenario(scenario_id, builder=builder)
        slowest = max(slowest, report.slowest_ms)
        print(f"\n{'=' * 96}")
        print(f"{scenario_id}  {report.replay.scenario_name}")
        print(f'  "{report.replay.cardholder_instruction}"')
        print()
        for step in report.steps:
            auth, verdict = step.event.authorization, step.verdict
            print(
                f"  {MARK[verdict.decision.value]} #{auth.replay_order:<2} "
                f"{auth.source_authorization_id}  CHF {auth.billing_amount_chf!s:>7}  "
                f"{auth.merchant.merchant_name[:20]:<20} {verdict.elapsed_ms:5.2f}ms"
            )
            print(f"      {verdict.customer_message}")
            if args.evidence:
                for finding in verdict.findings:
                    if finding.outcome.value != "pass":
                        print(f"        [{finding.outcome.value}] {finding.code}: {finding.detail}")
        counts = report.counts
        print(
            f"\n  {counts['approve']} approved, {counts['decline']} declined, "
            f"{counts['step_up']} sent to the customer"
        )

    print(f"\nSlowest decision: {slowest:.2f} ms (the platform allows 8000 ms).")
    return 0


def cmd_demo(_: argparse.Namespace) -> int:
    """Show the three things the brief asks a demo to show."""
    from .replay.demo import build_demo

    demo = build_demo()
    for index, moment in enumerate(demo["moments"], start=1):
        print(f"\n{'=' * 92}")
        print(f"{index}. {moment.heading}")
        print(
            f"   {moment.source_id} at {moment.merchant} for CHF {moment.amount_chf:.2f} "
            f"-> {moment.decision.upper()} in {moment.elapsed_ms:.2f} ms"
        )
        print(f"\n   {moment.message}")
        if moment.evidence:
            print("\n   What it considered:")
            for outcome, code, detail in moment.evidence:
                print(f"     [{outcome}] {code}")
                print(f"           {detail}")

    print(f"\n{'=' * 92}")
    print("The customer keeps control")
    for label, allowed, problem in demo["policy_control"]:
        mark = "allowed" if allowed else "REFUSED"
        print(f"   {label:<34} {mark}")
        if problem:
            print(f"      {problem}")

    print("\n   What we refuse to decide for them:")
    for question in demo["open_questions"][:3]:
        print(f"     ? {question}")

    print(f"\nSlowest decision across all 45 purchases: {demo['slowest_ms']:.2f} ms "
          "(the platform allows 8000 ms).\n")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Run the live worker against the sandbox. Needs TEAM_API_KEY."""
    from .api import ApiError, LeashClient
    from .worker import Worker

    try:
        with LeashClient() as client:
            worker = Worker(client)
            print(f"polling {client.settings.base_url} ...")
            stats = worker.run_until_idle(wait=args.wait, max_empty_polls=args.max_empty)
    except ApiError as exc:
        print(f"worker stopped: {exc}")
        return 1

    print(
        f"\n{stats.decided} decided ({stats.replayed} repeats), "
        f"{stats.by_decision['approve']} approved, {stats.by_decision['decline']} declined, "
        f"{stats.by_decision['step_up']} sent to the customer"
    )
    print(f"{stats.polled} polls, {stats.empty_polls} empty; slowest {stats.slowest_ms:.2f} ms")
    if stats.parse_failures:
        print(f"{stats.parse_failures} request(s) could not be read and were escalated")
    if stats.missed_deadlines:
        print(f"WARNING: {stats.missed_deadlines} decision(s) finished past the deadline")
    return 0


COMMANDS = {
    "check": cmd_check,
    "health": cmd_health,
    "validate": cmd_validate,
    "policy": cmd_policy,
    "replay": cmd_replay,
    "demo": cmd_demo,
    "worker": cmd_worker,
}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="leash", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in COMMANDS.items():
        parser_ = sub.add_parser(name, help=(fn.__doc__ or "").strip().splitlines()[0])
        if name == "validate":
            parser_.add_argument("paths", nargs="+", help="JSON event or envelope files")
        if name == "worker":
            parser_.add_argument("--wait", type=int, default=25, help="long-poll seconds")
            parser_.add_argument("--max-empty", type=int, default=3, dest="max_empty")
        if name == "replay":
            parser_.add_argument("--scenario", help="one scenario id; default is all")
            parser_.add_argument("--evidence", action="store_true", help="show every finding")
        if name == "policy":
            parser_.add_argument("instruction", nargs="?", help="the customer's own words")
            parser_.add_argument("--scenario", default="SCEN0000", help="use a catalogue instruction")
            parser_.add_argument("--json", action="store_true", help="print the draft request body")
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
