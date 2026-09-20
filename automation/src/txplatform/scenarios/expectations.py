"""Evaluates what a scenario's traffic actually produced against its application expectations.

Pure functions: responses, order states and ledger counts go in, check results come out. The engine
gathers the facts; these rules decide pass or fail.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from txplatform.scenarios.schema import ApplicationExpectations, CountRule


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def count_matches(rule: CountRule, count: int, total: int) -> bool:
    if rule.all:
        return count == total and total > 0
    if rule.exactly is not None and count != rule.exactly:
        return False
    if rule.min is not None and count < rule.min:
        return False
    return not (rule.max is not None and count > rule.max)


def describe(rule: CountRule) -> str:
    if rule.all:
        return "all"
    parts = []
    if rule.exactly is not None:
        parts.append(f"exactly {rule.exactly}")
    if rule.min is not None:
        parts.append(f"at least {rule.min}")
    if rule.max is not None:
        parts.append(f"at most {rule.max}")
    return " and ".join(parts)


def evaluate_statuses(expectations: ApplicationExpectations, statuses: list[int]) -> CheckResult:
    counts = Counter(statuses)
    problems = [
        f"{status}: {counts.get(status, 0)} (expected {describe(rule)})"
        for status, rule in expectations.statuses.items()
        if not count_matches(rule, counts.get(status, 0), len(statuses))
    ]
    unexpected = sorted(status for status in counts if status not in expectations.statuses)
    if unexpected:
        problems.append(f"unexpected statuses {({s: counts[s] for s in unexpected})}")
    summary = ", ".join(f"{status}x{count}" for status, count in sorted(counts.items()))
    return CheckResult("HTTP statuses", not problems, "; ".join(problems) or summary)


def evaluate_states(expectations: ApplicationExpectations, states: list[str]) -> CheckResult | None:
    if not expectations.states:
        return None
    counts = Counter(states)
    problems = [
        f"{state}: {counts.get(state, 0)} (expected {describe(rule)})"
        for state, rule in expectations.states.items()
        if not count_matches(rule, counts.get(state, 0), len(states))
    ]
    unexpected = sorted(state for state in counts if state not in expectations.states)
    if unexpected:
        problems.append(f"unexpected states {({s: counts[s] for s in unexpected})}")
    summary = ", ".join(f"{state}x{count}" for state, count in sorted(counts.items()))
    return CheckResult("order states", not problems, "; ".join(problems) or summary or "no orders created")


def evaluate_compensation(expectations: ApplicationExpectations, failed_orders: list[dict]) -> CheckResult | None:
    if expectations.compensation is None:
        return None
    required = list(expectations.compensation)
    problems = []
    for order in failed_orders:
        done = [step["action"] for step in order.get("compensation", []) if step.get("status") == "Succeeded"]
        if done != required:
            problems.append(f"{order.get('orderId')}: {done}")
    detail = f"{len(failed_orders)} failed orders compensated with {required}" if not problems else f"expected {required}, got {problems[:5]}"
    return CheckResult("compensation", not problems and bool(failed_orders or not required), detail)


def evaluate_authorizations(expectations: ApplicationExpectations, per_order: dict[str, int]) -> CheckResult | None:
    if expectations.authorizations_per_order is None:
        return None
    wrong = {order: count for order, count in per_order.items() if count != expectations.authorizations_per_order}
    detail = (f"{len(per_order)} orders with exactly {expectations.authorizations_per_order} authorization(s)"
              if not wrong else f"unexpected authorization counts: {dict(list(wrong.items())[:5])}")
    return CheckResult("payment authorizations", not wrong and bool(per_order), detail)
