"""`platformctl`: command-line entry point for the platform lifecycle.

Commands: preflight, bootstrap, deploy, validate, status, ui, destroy.
Scenario experiments live in the separate `scenarioctl` CLI.
"""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import nullcontext

from txplatform import console, kind, kube, lifecycle, portforward, preflight, runlock, status
from txplatform.validation import framework, suites


def _cmd_preflight(_: argparse.Namespace) -> int:
    findings = preflight.run()
    for finding in findings:
        line = f"{finding.check}: {finding.message}"
        {preflight.Level.PASS: console.ok, preflight.Level.WARN: console.warn, preflight.Level.FAIL: console.fail}[finding.level](line)
    return 1 if any(f.level is preflight.Level.FAIL for f in findings) else 0


def _platform_lock(purpose: str):
    """Deployments restart pods and validation sends traffic, so neither may overlap a scenario run
    whose checks compare exact counts. The lock lives in the application namespace; before that
    namespace exists, no experiment can run either."""
    if kind.cluster_exists() and kube.namespace_exists(kube.NS_APP):
        return runlock.held(purpose)
    return nullcontext()


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    with _platform_lock("bootstrap"):
        if not lifecycle.bootstrap():
            return 1
    if args.skip_validation:
        return 0
    return _run_suites(suites.smoke_suite_names())


def _cmd_deploy(args: argparse.Namespace) -> int:
    with _platform_lock("deployment"):
        lifecycle.deploy(args.component or lifecycle.component_names())
    return 0


def _run_suites(names: list[str]) -> int:
    outcomes: list[framework.CheckOutcome] = []
    try:
        with _platform_lock(f"validation of {', '.join(names)}"):
            for name in names:
                outcomes.extend(framework.run_suite(suites.build(name)))
    except runlock.LockHeldError as error:
        console.fail(str(error))
        return 1
    return 0 if framework.summarize(outcomes) else 1


def _cmd_validate(args: argparse.Namespace) -> int:
    return _run_suites(args.suite or suites.default_suite_names())


def _cmd_status(_: argparse.Namespace) -> int:
    return 0 if status.show() else 1


def _cmd_destroy(args: argparse.Namespace) -> int:
    lifecycle.destroy(purge=args.purge)
    return 0


# Operator UIs stay cluster-internal; they are opened on loopback through kubectl port-forward.
UI_TARGETS = {
    "jaeger": (kube.NS_OBSERVABILITY, "svc/jaeger-query", 16686, 16686, "/search"),
    "prometheus": (kube.NS_OBSERVABILITY, "svc/prometheus-server", 9090, 9090, "/targets"),
}


def _cmd_ui(args: argparse.Namespace) -> int:
    namespace, target, remote_port, local_port, path = UI_TARGETS[args.target]
    with portforward.forward(namespace, target, remote_port, local_port=args.port or local_port) as port:
        console.ok(f"{args.target} is available at http://127.0.0.1:{port}{path} (press Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            console.info("port-forward stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="platformctl", description="Manage the local transaction observability platform.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("preflight", help="check tools and workstation resources").set_defaults(handler=_cmd_preflight)

    bootstrap = commands.add_parser("bootstrap", help="create the cluster and deploy every component")
    bootstrap.add_argument("--skip-validation", action="store_true", help="skip the smoke validation at the end")
    bootstrap.set_defaults(handler=_cmd_bootstrap)

    deploy = commands.add_parser("deploy", help="deploy selected components to the existing cluster")
    deploy.add_argument("--component", action="append", choices=lifecycle.component_names(), help="repeat to select several; default is all")
    deploy.set_defaults(handler=_cmd_deploy)

    validate = commands.add_parser("validate", help="run operational validation suites against the running platform")
    validate.add_argument("--suite", action="append", choices=suites.all_suite_names(), help="repeat to select several; default is all non-disruptive suites")
    validate.set_defaults(handler=_cmd_validate)

    commands.add_parser("status", help="show nodes, releases and workload readiness").set_defaults(handler=_cmd_status)

    ui = commands.add_parser("ui", help="open Jaeger or Prometheus on localhost through a port-forward")
    ui.add_argument("target", choices=sorted(UI_TARGETS))
    ui.add_argument("--port", type=int, default=0, help="local port (default: 16686 for Jaeger, 9090 for Prometheus)")
    ui.set_defaults(handler=_cmd_ui)

    destroy = commands.add_parser("destroy", help="delete the kind cluster")
    destroy.add_argument("--purge", action="store_true", help="also delete generated certificates in .local")
    destroy.set_defaults(handler=_cmd_destroy)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        console.fail("interrupted")
        return 130
    except Exception as error:  # noqa: BLE001 - print a clear message instead of a stack trace
        console.fail(f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
