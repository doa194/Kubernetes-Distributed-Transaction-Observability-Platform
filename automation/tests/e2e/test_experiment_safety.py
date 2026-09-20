"""End-to-end tests of experiment safety: one experiment at a time, and a crashed run can always be undone.

These run real `scenarioctl` processes against the deployed platform, because the behavior under
test is what happens between processes: a lock held by another process, and a process that dies
in the middle of a change to the cluster.
"""

import json
import subprocess
import sys

import pytest

from txplatform import kube, paths, runlock
from txplatform.scenarios import control, engine, records

pytestmark = pytest.mark.e2e

SCENARIOCTL = [sys.executable, "-m", "txplatform.scenario_cli"]


def _journal_kinds(run_id: str) -> list[str]:
    journal = paths.runs_dir() / run_id / "journal.jsonl"
    if not journal.is_file():
        return []
    return [json.loads(line)["kind"] for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_second_experiment_is_refused_while_another_process_holds_the_lock():
    with runlock.held("e2e lock holder"):
        result = subprocess.run([*SCENARIOCTL, "run", "normal-order", "--skip-telemetry"], capture_output=True, text=True, timeout=120)

    assert result.returncode == 1
    assert "holds the experiment lock" in result.stdout + result.stderr


def test_killed_run_is_restored_by_reset():
    previous_run = records.latest_run_id("dependency-unavailable")

    def new_run_with_scale_journaled() -> str | None:
        latest = records.latest_run_id("dependency-unavailable")
        return latest if latest != previous_run and "scale" in _journal_kinds(latest) else None

    process = subprocess.Popen([*SCENARIOCTL, "run", "dependency-unavailable", "--skip-telemetry"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Wait until the run has journaled and made its disruptive change (ShippingService scaled to zero).
        run_id = kube.wait_for(new_run_with_scale_journaled, timeout=120, interval=0.5, description="journaled scale action")
        kube.wait_for(lambda: control.current_replicas("shipping-service") == 0, timeout=60, description="shipping-service scaled to zero")
    finally:
        # A hard kill: no cleanup code of the run gets a chance to execute.
        process.kill()
        process.wait(timeout=30)

    assert not records.load(run_id).restored
    actions = engine.reset(force=True)

    assert f"restored interrupted run {run_id}" in actions
    assert control.current_replicas("shipping-service") == 1
    assert kube.deployment_available(kube.NS_APP, "shipping-service")
    assert records.load(run_id).restored
    assert runlock.current() is None
