"""Reusable checks that decide whether a trace truthfully describes what happened.

Every function is pure: it receives parsed traces (and expectations) and returns a list of problems;
an empty list means the check passed. Timing comparisons only use spans recorded by the same
process (OrderService), so clock differences between nodes cannot produce false results.
"""

from __future__ import annotations

import json
import re

from txplatform.traces import Span, Trace

EDGE_SERVICE = "kong-gateway"
ORDER_SERVICE = "order-service"
DOWNSTREAM = ("inventory-service", "fraud-service", "payment-service", "shipping-service")
ALLOWED_EDGES = {(EDGE_SERVICE, ORDER_SERVICE)} | {(ORDER_SERVICE, service) for service in DOWNSTREAM}
DOTNET_SERVICES = (ORDER_SERVICE, *DOWNSTREAM)
K8S_KEYS = ("k8s.namespace.name", "k8s.pod.name", "k8s.node.name", "k8s.deployment.name")
# Mirrors the Collector's deletion rule for attribute names that suggest credentials or card data.
SENSITIVE_KEY_PATTERN = re.compile(r"(?i)(authorization|cookie|password|passwd|secret|token|api[._-]?key|card[._-]?number)")
STAGES = ("inventory.reserve", "fraud.evaluate", "payment.authorize", "shipping.create")
COMPENSATION_STAGES = ("compensation.payment.void", "compensation.inventory.release")


def _single(trace: Trace, name: str) -> Span | None:
    matches = trace.named(name)
    return matches[0] if len(matches) == 1 else None


def transaction(trace: Trace) -> Span | None:
    return _single(trace, "order.transaction")


def gateway_span_gaps(trace: Trace) -> list[Span]:
    """OrderService spans whose missing parent is explained by a known Kong 3.9 limitation.

    When the upstream answers with a 5xx status, Kong marks its last try as failed
    (kong/runloop/handler.lua) and the tracing code then starts a new kong.balancer span instead
    of finishing the one whose id it already sent upstream in traceparent
    (kong/observability/tracing/instrumentation.lua). The OrderService server span therefore points
    at a span id Kong never exports. Only that exact pattern is accepted.
    """
    known = {span.span_id for span in trace.spans}
    balancers = [span for span in trace.by_service(EDGE_SERVICE, "CLIENT") if span.name == "kong.balancer"]
    kong_roots = [span for span in trace.by_service(EDGE_SERVICE, "SERVER") if span.is_root]
    if len(balancers) != 1 or len(kong_roots) != 1:
        return []
    balancer, kong_root = balancers[0], kong_roots[0]
    failed_upstream = balancer.status == "ERROR" and int(balancer.attributes.get("http.response.status_code") or 0) >= 500
    if not failed_upstream:
        return []
    return [
        span for span in trace.by_service(ORDER_SERVICE, "SERVER")
        if span.parent_span_id not in known
        and span.attributes.get("kong.request.id") == kong_root.attributes.get("kong.request.id")
        and kong_root.start_unix_nano <= span.start_unix_nano
    ]


def integrity(trace: Trace) -> list[str]:
    problems = []
    known = {span.span_id for span in trace.spans}
    explained = {span.span_id for span in gateway_span_gaps(trace)}
    orphans = [span.name for span in trace.spans if span.parent_span_id and span.parent_span_id not in known and span.span_id not in explained]
    if orphans:
        problems.append(f"spans whose parent is missing: {orphans}")
    roots = [span for span in trace.spans if not span.parent_span_id]
    if len(roots) != 1:
        problems.append(f"expected exactly one root span, found {len(roots)}")
    elif roots[0].service != EDGE_SERVICE:
        problems.append(f"root span belongs to {roots[0].service}, not {EDGE_SERVICE}")
    return problems


def service_edges(trace: Trace) -> set[tuple[str, str]]:
    edges = set()
    for span in trace.spans:
        parent = trace.parent_of(span)
        if parent is not None and parent.service != span.service:
            edges.add((parent.service, span.service))
    return edges


def topology(trace: Trace, required: set[str], forbidden: set[str]) -> list[str]:
    problems = []
    missing = required - trace.services
    if missing:
        problems.append(f"missing services {sorted(missing)}")
    present = forbidden & trace.services
    if present:
        problems.append(f"services that must not run were called: {sorted(present)}")
    unexpected = service_edges(trace) - ALLOWED_EDGES
    if unexpected:
        problems.append(f"calls outside the allowed topology: {sorted(unexpected)}")
    return problems


def stages_present(trace: Trace, required: tuple[str, ...], forbidden: tuple[str, ...] = ()) -> list[str]:
    problems = [f"stage {name} missing" for name in required if not trace.named(name)]
    problems += [f"stage {name} must not run" for name in forbidden if trace.named(name)]
    return problems


def stage_order(trace: Trace) -> list[str]:
    """Inventory and fraud run concurrently; payment starts after both; shipping after payment;
    compensation after the failed stage."""
    problems = []
    inventory, fraud = _single(trace, "inventory.reserve"), _single(trace, "fraud.evaluate")
    if inventory is None or fraud is None:
        return ["inventory.reserve and fraud.evaluate must each appear exactly once"]
    if not inventory.overlaps(fraud):
        problems.append("inventory.reserve and fraud.evaluate did not overlap in time")
    payment = _single(trace, "payment.authorize")
    fan_out_end = max(inventory.end_unix_nano, fraud.end_unix_nano)
    if payment is not None and payment.start_unix_nano < fan_out_end:
        problems.append("payment.authorize started before both parallel checks finished")
    shipping = _single(trace, "shipping.create")
    if shipping is not None and payment is not None and shipping.start_unix_nano < payment.end_unix_nano:
        problems.append("shipping.create started before payment.authorize finished")
    last_business_end = max((s.end_unix_nano for name in STAGES for s in trace.named(name)), default=0)
    for name in COMPENSATION_STAGES:
        for span in trace.named(name):
            if span.start_unix_nano < last_business_end:
                problems.append(f"{name} started before the failing stage finished")
    return problems


def state_path(trace: Trace, expected_path: list[str]) -> list[str]:
    tx = transaction(trace)
    if tx is None:
        return ["order.transaction span missing"]
    path = [event.attributes.get("order.state.to") for event in tx.events if event.name == "order.state_changed"]
    problems = []
    if path != expected_path:
        problems.append(f"state events {path} != expected {expected_path}")
    if tx.attributes.get("order.state") != expected_path[-1]:
        problems.append(f"order.state is {tx.attributes.get('order.state')}, expected {expected_path[-1]}")
    return problems


def error_marking(trace: Trace, technical_failure: bool) -> list[str]:
    tx = transaction(trace)
    if tx is None:
        return ["order.transaction span missing"]
    if technical_failure and tx.status != "ERROR":
        return ["the transaction span is not marked as an error"]
    if not technical_failure and tx.status == "ERROR":
        return ["the transaction span is marked as an error although no technical failure happened"]
    return []


def attempts(trace: Trace, stage_name: str) -> list[Span]:
    stage = _single(trace, stage_name)
    if stage is None:
        return []
    return sorted((child for child in trace.children_of(stage) if child.kind == "CLIENT"), key=lambda s: s.start_unix_nano)


def retries(trace: Trace, stage_name: str, expected_attempts: int) -> list[str]:
    problems = []
    stage = _single(trace, stage_name)
    if stage is None:
        return [f"stage {stage_name} missing"]
    client_spans = attempts(trace, stage_name)
    numbers = [span.attributes.get("retry.attempt") for span in client_spans]
    if numbers != list(range(1, expected_attempts + 1)):
        problems.append(f"{stage_name} attempts {numbers}, expected 1..{expected_attempts}")
    retry_events = [event for event in stage.events if event.name == "retry"]
    if len(retry_events) != expected_attempts - 1:
        problems.append(f"{len(retry_events)} retry events on {stage_name}, expected {expected_attempts - 1}")
    if expected_attempts > 1 and stage.attributes.get("retry.count") != expected_attempts - 1:
        problems.append(f"retry.count is {stage.attributes.get('retry.count')}, expected {expected_attempts - 1}")
    return problems


def idempotent_replay(trace: Trace) -> list[str]:
    servers = sorted((s for s in trace.by_service("payment-service", "SERVER") if "authorizations" in s.name), key=lambda s: s.start_unix_nano)
    if len(servers) < 2:
        return [f"expected at least two authorization attempts at PaymentService, found {len(servers)}"]
    problems = []
    recorded = sum(1 for span in servers for event in span.events if event.name == "payment.authorization_recorded")
    if recorded != 1:
        problems.append(f"the authorization was recorded {recorded} times, expected exactly once")
    if servers[-1].attributes.get("payment.idempotent_replay") is not True:
        problems.append("the final attempt was not answered as an idempotent replay")
    return problems


def latency_dominance(trace: Trace, stage_name: str, min_share: float, min_trace_ms: float) -> list[str]:
    tx, stage = transaction(trace), _single(trace, stage_name)
    if tx is None or stage is None:
        return [f"order.transaction or {stage_name} missing"]
    problems = []
    share = stage.duration_ms / tx.duration_ms if tx.duration_ms else 0
    if share < min_share:
        problems.append(f"{stage_name} takes {share:.0%} of the transaction, expected at least {min_share:.0%}")
    longest = max((child for child in trace.children_of(tx)), key=lambda s: s.duration_ms)
    if longest.span_id != stage.span_id:
        problems.append(f"the longest step is {longest.name}, not {stage_name}")
    roots = [span for span in trace.spans if not span.parent_span_id]
    if roots and roots[0].duration_ms < min_trace_ms:
        problems.append(f"trace lasted {roots[0].duration_ms:.0f} ms, expected at least {min_trace_ms:.0f} ms")
    return problems


def kubernetes_metadata(trace: Trace) -> list[str]:
    problems = []
    for span in trace.spans:
        if span.service in DOTNET_SERVICES:
            missing = [key for key in K8S_KEYS if not span.resource.get(key)]
            if missing:
                problems.append(f"{span.service}/{span.name} lacks {missing}")
        elif span.service == EDGE_SERVICE and not span.resource.get("k8s.namespace.name"):
            problems.append("Kong spans lack k8s.namespace.name")
    return problems[:5]


def correlation(trace: Trace, request: dict, run_id: str) -> list[str]:
    problems = []
    servers = trace.by_service(ORDER_SERVICE, "SERVER")
    if not servers:
        return ["OrderService server span missing"]
    server = servers[0]
    expectations = {
        "correlation.id": request["correlation_id"],
        "scenario.run_id": run_id,
        "kong.request.id": request["kong_request_id"],
    }
    for key, expected in expectations.items():
        if expected and server.attributes.get(key) != expected:
            problems.append(f"{key} is {server.attributes.get(key)!r}, expected {expected!r}")
    tx = transaction(trace)
    if request.get("order_id") and tx is not None and tx.attributes.get("order.id") != request["order_id"]:
        problems.append(f"order.id is {tx.attributes.get('order.id')!r}, expected {request['order_id']!r}")
    downstream_run_ids = {s.attributes.get("scenario.run_id") for s in trace.spans if s.service in DOWNSTREAM and s.kind == "SERVER"}
    if downstream_run_ids and downstream_run_ids != {run_id}:
        problems.append(f"downstream spans carry scenario.run_id {downstream_run_ids}")
    return problems


def sensitive_data(trace: Trace, canaries: list[str]) -> list[str]:
    problems = []
    for span in trace.spans:
        for key in span.attributes:
            if SENSITIVE_KEY_PATTERN.search(key):
                problems.append(f"{span.service}/{span.name} has attribute {key}")
    dump = json.dumps([[s.attributes, [e.attributes for e in s.events], s.resource] for s in trace.spans])
    problems += [f"canary value {canary[:12]}... found in telemetry" for canary in canaries if canary and canary in dump]
    return problems
