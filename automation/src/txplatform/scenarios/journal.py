"""Write-ahead journal of every change a scenario makes, and the plan to undo those changes.

Each change is written to disk *before* it is made. If the runner crashes or is interrupted, the
journal still lists everything that may have happened, so `scenarioctl reset` can restore the
platform. The restore plan is derived purely from the journal and is safe to run more than once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RestoreStep:
    kind: str  # "scale", "clear-faults", "wait-available"
    target: str
    replicas: int | None = None


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, kind: str, **details: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **details}) + "\n")
            stream.flush()

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


def restore_plan(entries: list[dict[str, Any]]) -> list[RestoreStep]:
    """Undo steps for journaled changes, in a safe order: remove faults first, then restore replica
    counts, then wait until every touched workload is available again (a pod that lost its
    readiness needs a moment to report ready after its fault is removed)."""
    fault_services: list[str] = []
    original_replicas: dict[str, int] = {}
    touched: list[str] = []
    for entry in entries:
        kind = entry["kind"]
        workload = entry.get("service") if kind == "fault" else entry.get("workload")
        if kind == "fault" and workload not in fault_services:
            fault_services.append(workload)
        elif kind == "scale":
            # The first recorded count is the original; later scale actions must not overwrite it.
            original_replicas.setdefault(workload, int(entry["originalReplicas"]))
        if kind in {"fault", "scale", "rollout-restart", "delete-pod"} and workload not in touched:
            touched.append(workload)
    steps = [RestoreStep("clear-faults", service) for service in fault_services]
    steps += [RestoreStep("scale", workload, replicas) for workload, replicas in original_replicas.items()]
    steps += [RestoreStep("wait-available", workload) for workload in touched]
    return steps
