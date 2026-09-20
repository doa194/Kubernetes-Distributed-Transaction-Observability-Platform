"""The record of one scenario run, stored as JSON under .runs/<run id>/.

It keeps every identifier needed to verify the run again later: correlation ids, Kong request ids,
trace ids and order ids per request, plus timestamps, check results and cleanup status.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from txplatform import paths


def new_run_id(scenario_id: str) -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{scenario_id}-{secrets.token_hex(2)}"


@dataclass
class RunRecord:
    run_id: str
    scenario_id: str
    status: str = "running"  # running | passed | failed | interrupted | error
    started_at: str = ""
    finished_at: str = ""
    workload_started_at: float = 0.0
    workload_finished_at: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    kubernetes_events: list[dict[str, Any]] = field(default_factory=list)
    faults: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    metrics_before: dict[str, float] = field(default_factory=dict)
    restored: bool = False
    error: str = ""

    @property
    def directory(self) -> Path:
        return paths.runs_dir() / self.run_id

    @property
    def journal_path(self) -> Path:
        return self.directory / "journal.jsonl"

    def add_check(self, name: str, passed: bool, detail: str, layer: str = "application") -> None:
        self.checks.append({"layer": layer, "name": name, "passed": passed, "detail": detail})

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check["passed"] for check in self.checks)

    def save(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / "record.json"
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return target


def load(run_id: str) -> RunRecord:
    data = json.loads((paths.runs_dir() / run_id / "record.json").read_text(encoding="utf-8"))
    return RunRecord(**data)


def unrestored_runs() -> list[RunRecord]:
    if not paths.runs_dir().is_dir():
        return []
    found = []
    for record_file in sorted(paths.runs_dir().glob("*/record.json")):
        record = load(record_file.parent.name)
        if not record.restored:
            found.append(record)
    return found


def latest_run_id(scenario_id: str | None = None) -> str | None:
    if not paths.runs_dir().is_dir():
        return None
    candidates = sorted(p.parent.name for p in paths.runs_dir().glob("*/record.json") if scenario_id is None or f"-{scenario_id}-" in p.parent.name)
    return candidates[-1] if candidates else None
