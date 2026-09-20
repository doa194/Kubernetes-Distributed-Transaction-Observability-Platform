"""Runs validation checks and reports their results.

A check is a function that returns a short success detail, or raises `CheckFailed`
(or any other exception) to fail with a reason. One failing check never stops the
rest of the suite, so a single run shows every problem at once.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from kubernetes.client.exceptions import ApiException

from txplatform import console

CheckFunction = Callable[[], str | None]


class CheckFailed(AssertionError):
    """A check found behavior that does not match the expectation."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def describe_error(error: Exception) -> str:
    """One readable line per error; Kubernetes API errors otherwise print every HTTP header."""
    if isinstance(error, ApiException):
        message = error.reason
        try:
            message = json.loads(error.body or "{}").get("message", message)
        except ValueError:
            pass
        return f"Kubernetes API {error.status}: {message}"
    return f"{type(error).__name__}: {error}"


@dataclass(frozen=True)
class CheckOutcome:
    suite: str
    name: str
    passed: bool
    detail: str
    seconds: float


@dataclass
class Suite:
    name: str
    description: str
    checks: list[tuple[str, CheckFunction]] = field(default_factory=list)
    # Runs after all checks, even when some failed, to remove temporary resources.
    cleanup: Callable[[], None] | None = None

    def check(self, name: str) -> Callable[[CheckFunction], CheckFunction]:
        def register(function: CheckFunction) -> CheckFunction:
            self.checks.append((name, function))
            return function

        return register


def run_suite(suite: Suite) -> list[CheckOutcome]:
    console.step(f"Suite '{suite.name}': {suite.description}")
    outcomes: list[CheckOutcome] = []
    try:
        for name, function in suite.checks:
            started = time.monotonic()
            try:
                detail = function() or ""
                outcome = CheckOutcome(suite.name, name, True, detail, time.monotonic() - started)
                console.ok(f"{name} ({outcome.seconds:.1f}s){': ' + detail if detail else ''}")
            except CheckFailed as failure:
                outcome = CheckOutcome(suite.name, name, False, str(failure), time.monotonic() - started)
                console.fail(f"{name}: {failure}")
            except Exception as error:  # noqa: BLE001 - unexpected errors are reported as failures too
                detail = describe_error(error)
                outcome = CheckOutcome(suite.name, name, False, detail, time.monotonic() - started)
                console.fail(f"{name}: {detail}")
            outcomes.append(outcome)
    finally:
        if suite.cleanup:
            try:
                suite.cleanup()
            except Exception as error:  # noqa: BLE001 - cleanup problems must not hide check results
                console.warn(f"cleanup of suite '{suite.name}' failed: {error}")
    return outcomes


def summarize(outcomes: list[CheckOutcome]) -> bool:
    failed = [o for o in outcomes if not o.passed]
    console.step(f"{len(outcomes) - len(failed)} of {len(outcomes)} checks passed")
    for outcome in failed:
        console.fail(f"{outcome.suite} / {outcome.name}: {outcome.detail}")
    return not failed
