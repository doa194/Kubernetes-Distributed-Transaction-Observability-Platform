"""Trace checks must accept real traces and reject each specific kind of damage.

The fixtures are traces stored by Jaeger during real scenario runs (normal-order, payment-retry
and payment-timeout). Every negative test changes one detail of a real trace, so a check that
stopped looking at that detail would be caught here instead of passing silently on the platform.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from txplatform import traces
from txplatform.scenarios import tracechecks
from txplatform.traces import Span, Trace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "traces"
FULL_PATH = ["InventoryReserved", "PaymentAuthorized", "ShipmentCreated", "Completed"]


def _load(name: str) -> tuple[Trace, dict, str]:
    document = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    context = json.loads((FIXTURES / f"{name}.request.json").read_text(encoding="utf-8"))
    (trace,) = traces.group_traces(traces.parse_spans(document))
    return trace, context["request"], context["run_id"]


@pytest.fixture(scope="module")
def normal() -> tuple[Trace, dict, str]:
    return _load("normal-order")


@pytest.fixture(scope="module")
def retried() -> tuple[Trace, dict, str]:
    return _load("payment-retry")


@pytest.fixture(scope="module")
def timed_out() -> tuple[Trace, dict, str]:
    return _load("payment-timeout")


def _only(trace: Trace, name: str, service: str | None = None, kind: str | None = None) -> Span:
    (span,) = [s for s in trace.spans if s.name == name and (service is None or s.service == service) and (kind is None or s.kind == kind)]
    return span


def _replace(trace: Trace, old: Span, **changes) -> Trace:
    new = dataclasses.replace(old, **changes)
    return Trace(trace.trace_id, tuple(new if span is old else span for span in trace.spans))


def _without(trace: Trace, removed: Span) -> Trace:
    return Trace(trace.trace_id, tuple(span for span in trace.spans if span is not removed))


def test_real_normal_order_trace_passes_every_check(normal):
    trace, request, run_id = normal

    problems = (
        tracechecks.integrity(trace)
        + tracechecks.topology(trace, {"kong-gateway", *tracechecks.DOTNET_SERVICES}, set())
        + tracechecks.stage_order(trace)
        + tracechecks.state_path(trace, FULL_PATH)
        + tracechecks.error_marking(trace, technical_failure=False)
        + tracechecks.kubernetes_metadata(trace)
        + tracechecks.correlation(trace, request, run_id)
        + tracechecks.sensitive_data(trace, [f"canary-{run_id}"])
    )

    assert problems == []


def test_real_retried_payment_trace_shows_two_attempts_and_one_replay(retried):
    trace, _, _ = retried

    assert tracechecks.retries(trace, "payment.authorize", 2) == []
    assert tracechecks.idempotent_replay(trace) == []
    assert tracechecks.latency_dominance(trace, "payment.authorize", 0.5, 1500) == []


def test_missing_span_breaks_integrity(normal):
    trace, _, _ = normal

    problems = tracechecks.integrity(_without(trace, _only(trace, "order.transaction")))

    assert any("parent is missing" in p for p in problems)


def test_second_root_breaks_integrity(normal):
    trace, _, _ = normal
    server = _only(trace, "POST /orders", "order-service")

    problems = tracechecks.integrity(_replace(trace, server, parent_span_id=""))

    assert any("exactly one root" in p for p in problems)


def test_trace_not_rooted_at_the_gateway_is_rejected(normal):
    trace, _, _ = normal
    without_kong = Trace(trace.trace_id, tuple(s for s in trace.spans if s.service != "kong-gateway"))
    server = _only(without_kong, "POST /orders", "order-service")

    problems = tracechecks.integrity(_replace(without_kong, server, parent_span_id=""))

    assert any("not kong-gateway" in p for p in problems)


def test_call_outside_the_allowed_topology_is_reported(normal):
    trace, _, _ = normal
    fraud = _only(trace, "POST /fraud/evaluations", "fraud-service")
    shipping = _only(trace, "POST /shipments", "shipping-service")

    problems = tracechecks.topology(_replace(trace, fraud, parent_span_id=shipping.span_id), set(), set())

    assert any("outside the allowed topology" in p for p in problems)


def test_payment_starting_before_the_parallel_checks_finished_is_reported(normal):
    trace, _, _ = normal
    fraud, payment = _only(trace, "fraud.evaluate"), _only(trace, "payment.authorize")

    problems = tracechecks.stage_order(_replace(trace, payment, start_unix_nano=fraud.end_unix_nano - 1))

    assert "payment.authorize started before both parallel checks finished" in problems


def test_sequential_inventory_and_fraud_checks_are_reported(normal):
    trace, _, _ = normal
    inventory, fraud = _only(trace, "inventory.reserve"), _only(trace, "fraud.evaluate")
    duration = fraud.end_unix_nano - fraud.start_unix_nano

    moved = _replace(trace, fraud, start_unix_nano=inventory.end_unix_nano + 1, end_unix_nano=inventory.end_unix_nano + 1 + duration)

    assert "inventory.reserve and fraud.evaluate did not overlap in time" in tracechecks.stage_order(moved)


def test_missing_state_event_is_reported(normal):
    trace, _, _ = normal
    tx = _only(trace, "order.transaction")

    problems = tracechecks.state_path(_replace(trace, tx, events=tx.events[:-1]), FULL_PATH)

    assert any("state events" in p for p in problems)


def test_business_trace_marked_as_error_is_reported(normal):
    trace, _, _ = normal

    assert tracechecks.error_marking(trace, technical_failure=True) == ["the transaction span is not marked as an error"]


def test_wrong_attempt_numbers_are_reported(retried):
    trace, _, _ = retried
    second = next(s for s in tracechecks.attempts(trace, "payment.authorize") if s.attributes.get("retry.attempt") == 2)

    renumbered = _replace(trace, second, attributes={**second.attributes, "retry.attempt": 1})

    assert any("attempts [1, 1]" in p for p in tracechecks.retries(renumbered, "payment.authorize", 2))
    assert tracechecks.retries(trace, "payment.authorize", 3) != []


def test_double_charge_is_visible_in_the_trace(retried):
    trace, _, _ = retried
    replay = next(s for s in trace.by_service("payment-service", "SERVER") if s.attributes.get("payment.idempotent_replay"))
    recorded = next(e for s in trace.spans for e in s.events if e.name == "payment.authorization_recorded")

    charged_twice = _replace(trace, replay, events=(*replay.events, recorded), attributes={**replay.attributes, "payment.idempotent_replay": False})

    problems = tracechecks.idempotent_replay(charged_twice)
    assert "the authorization was recorded 2 times, expected exactly once" in problems
    assert "the final attempt was not answered as an idempotent replay" in problems


def test_latency_rule_rejects_a_fast_trace(normal):
    trace, _, _ = normal

    problems = tracechecks.latency_dominance(trace, "payment.authorize", 0.6, 1000)

    assert any("expected at least 1000 ms" in p for p in problems)


def test_missing_pod_name_is_reported(normal):
    trace, _, _ = normal
    span = _only(trace, "POST /inventory/reservations", "inventory-service")
    resource = {k: v for k, v in span.resource.items() if k != "k8s.pod.name"}

    problems = tracechecks.kubernetes_metadata(_replace(trace, span, resource=resource))

    assert any("k8s.pod.name" in p for p in problems)


def test_broken_correlation_is_reported(normal):
    trace, request, run_id = normal

    assert any("correlation.id" in p for p in tracechecks.correlation(trace, {**request, "correlation_id": "other"}, run_id))
    assert any("scenario.run_id" in p for p in tracechecks.correlation(trace, request, "another-run"))


def test_secret_in_an_attribute_value_or_name_is_reported(normal):
    trace, _, run_id = normal
    span = _only(trace, "POST /orders", "order-service")
    canary = f"canary-{run_id}"

    leaked_value = _replace(trace, span, attributes={**span.attributes, "http.request.header.x-trace": canary})
    leaked_name = _replace(trace, span, attributes={**span.attributes, "http.request.header.authorization": "Bearer x"})

    assert any("canary value" in p for p in tracechecks.sensitive_data(leaked_value, [canary]))
    assert any("authorization" in p for p in tracechecks.sensitive_data(leaked_name, [canary]))


def test_real_failed_order_trace_is_intact_apart_from_the_known_gateway_gap(timed_out):
    trace, request, run_id = timed_out

    assert [span.name for span in tracechecks.gateway_span_gaps(trace)] == ["POST /orders"]
    assert tracechecks.integrity(trace) == []
    assert tracechecks.retries(trace, "payment.authorize", 3) == []
    assert tracechecks.state_path(trace, ["InventoryReserved", "PaymentFailed"]) == []
    assert tracechecks.error_marking(trace, technical_failure=True) == []
    assert tracechecks.correlation(trace, request, run_id) == []


def test_gateway_gap_is_only_accepted_when_kong_recorded_a_failed_upstream_answer(timed_out):
    trace, _, _ = timed_out
    balancer = _only(trace, "kong.balancer", "kong-gateway")

    successful_balancer = _replace(trace, balancer, status="UNSET", attributes={**balancer.attributes, "http.response.status_code": 201})

    assert tracechecks.gateway_span_gaps(successful_balancer) == []
    assert any("parent is missing" in p for p in tracechecks.integrity(successful_balancer))


def test_gateway_gap_is_not_accepted_for_a_different_request(timed_out):
    trace, _, _ = timed_out
    server = _only(trace, "POST /orders", "order-service")

    other_request = _replace(trace, server, attributes={**server.attributes, "kong.request.id": "another-request"})

    assert any("parent is missing" in p for p in tracechecks.integrity(other_request))
