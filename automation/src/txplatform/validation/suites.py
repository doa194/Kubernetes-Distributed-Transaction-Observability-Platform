"""Registry of validation suites.

`default` suites are safe to run at any time. `smoke` suites run at the end of bootstrap.
Disruptive suites (restarts, outages) are only run when selected explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from txplatform.validation import edge, foundation, observability, observability_resilience, recovery, security, telemetry_pipeline, tracing
from txplatform.validation.framework import Suite


@dataclass(frozen=True)
class SuiteEntry:
    build: Callable[[], Suite]
    default: bool
    smoke: bool


_REGISTRY: dict[str, SuiteEntry] = {
    "foundation": SuiteEntry(foundation.build, default=True, smoke=True),
    "security": SuiteEntry(security.build, default=True, smoke=True),
    "tracing": SuiteEntry(tracing.build, default=True, smoke=True),
    "edge": SuiteEntry(edge.build, default=True, smoke=True),
    "observability": SuiteEntry(observability.build, default=True, smoke=True),
    "telemetry-pipeline": SuiteEntry(telemetry_pipeline.build, default=True, smoke=False),
    "observability-resilience": SuiteEntry(observability_resilience.build, default=False, smoke=False),
    "recovery": SuiteEntry(recovery.build, default=False, smoke=False),
}


def all_suite_names() -> list[str]:
    return list(_REGISTRY)


def default_suite_names() -> list[str]:
    return [name for name, entry in _REGISTRY.items() if entry.default]


def smoke_suite_names() -> list[str]:
    return [name for name, entry in _REGISTRY.items() if entry.smoke]


def build(name: str) -> Suite:
    return _REGISTRY[name].build()
