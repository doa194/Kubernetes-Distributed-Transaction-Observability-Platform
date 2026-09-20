"""`scenarioctl`: runs and verifies fault, workload and security scenarios.

Commands: list, show, run, verify, report, reset.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from txplatform import console
from txplatform.scenarios import catalog, contracts, engine, records


def _print_record(record: records.RunRecord) -> None:
    statuses = Counter(r["status"] for r in record.requests)
    console.step(f"Run {record.run_id}: {record.status.upper()}")
    console.info(f"requests: {len(record.requests)} {dict(sorted(statuses.items()))}; orders read back: {len(record.orders)}")
    for event in record.kubernetes_events:
        console.info(f"kubernetes: {event}")
    for check in record.checks:
        line = f"[{check['layer']}] {check['name']}: {check['detail']}"
        (console.ok if check["passed"] else console.fail)(line)
    if record.error:
        console.fail(record.error.strip().splitlines()[0])
    console.info(f"record: {record.directory / 'record.json'}")


def _cmd_list(_: argparse.Namespace) -> int:
    for scenario in catalog.load_all().values():
        print(f"{scenario.id:<24} {scenario.category.value:<18} {scenario.title}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    scenario = catalog.get(args.scenario)
    print(json.dumps(scenario.model_dump(by_alias=True, exclude_none=True), indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    scenario = catalog.get(args.scenario)
    exit_code = 0
    for attempt in range(args.repeat):
        if args.repeat > 1:
            console.step(f"{scenario.id}: repetition {attempt + 1} of {args.repeat}")
        record = engine.run(scenario, verify_telemetry=not args.skip_telemetry)
        _print_record(record)
        exit_code |= 0 if record.status == "passed" else 1
    return exit_code


def _cmd_verify(args: argparse.Namespace) -> int:
    run_id = args.run or records.latest_run_id()
    if run_id is None:
        console.fail("no recorded runs")
        return 1
    record = records.load(run_id)
    scenario = catalog.get(record.scenario_id)
    record.checks = [c for c in record.checks if c["layer"] != "telemetry"]
    contracts.verify(scenario, record)
    record.status = "passed" if record.passed else "failed"
    record.save()
    _print_record(record)
    return 0 if record.status == "passed" else 1


def _cmd_report(args: argparse.Namespace) -> int:
    run_id = args.run or records.latest_run_id()
    if run_id is None:
        console.fail("no recorded runs")
        return 1
    _print_record(records.load(run_id))
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    for action in engine.reset(force=args.force):
        console.ok(action)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scenarioctl", description="Run fault, workload and security experiments against the platform.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list available scenarios").set_defaults(handler=_cmd_list)

    show = commands.add_parser("show", help="print a scenario definition")
    show.add_argument("scenario")
    show.set_defaults(handler=_cmd_show)

    run = commands.add_parser("run", help="run a scenario and verify application and telemetry results")
    run.add_argument("scenario")
    run.add_argument("--repeat", type=int, default=1, help="run the scenario several times in a row")
    run.add_argument("--skip-telemetry", action="store_true", help="only verify application results")
    run.set_defaults(handler=_cmd_run)

    verify = commands.add_parser("verify", help="verify the telemetry of a recorded run again")
    verify.add_argument("run", nargs="?", help="run id (default: latest run)")
    verify.set_defaults(handler=_cmd_verify)

    report = commands.add_parser("report", help="print a recorded run")
    report.add_argument("run", nargs="?", help="run id (default: latest run)")
    report.set_defaults(handler=_cmd_report)

    reset = commands.add_parser("reset", help="remove leftover faults, restore interrupted runs, close circuit breakers")
    reset.add_argument("--force", action="store_true", help="also release an experiment lock held by another process")
    reset.set_defaults(handler=_cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        console.fail("interrupted; the platform was restored where possible (run `scenarioctl reset` to be sure)")
        return 130
    except Exception as error:  # noqa: BLE001
        console.fail(f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
