"""Telemetry contracts: what the traces and trace-derived metrics of each scenario must show.

A contract answers "does the telemetry accurately describe what happened?". Trace checks read the
stored traces of the scenario's requests from the Jaeger Query API. Metric checks compare counters
before and after the run (or read a gauge over the run's time window); they cover traffic that
sampling deliberately does not keep, for example requests Kong rejected, and they are exact because
only one experiment runs at a time.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from txplatform import jaeger, prometheus
from txplatform.scenarios import records, tracechecks
from txplatform.scenarios.schema import Scenario
from txplatform.traces import Trace

# Tail-sampling decision wait (20 s) plus export, batching and index refresh delays.
DECISION_DELAY_SECONDS = 35
METRICS_SETTLE_SECONDS = 45

ALL_SERVICES = {tracechecks.EDGE_SERVICE, *tracechecks.DOTNET_SERVICES}
FULL_PATH = ["InventoryReserved", "PaymentAuthorized", "ShipmentCreated", "Completed"]


def _calls(**labels: str) -> str:
    return prometheus.metric_selector("traces_span_metrics_calls", **labels)


METRICS = {
    "kong_429": _calls(service_name="kong-gateway", span_name="kong", http_response_status_code="429"),
    "kong_413": _calls(service_name="kong-gateway", span_name="kong", http_response_status_code="413"),
    "order_posts": _calls(service_name="order-service", span_name="POST /orders", span_kind="SPAN_KIND_SERVER"),
    # Only POST /orders: the pre-flight check sends an unauthenticated GET that also ends with 401.
    "order_401": _calls(service_name="order-service", span_name="POST /orders", span_kind="SPAN_KIND_SERVER", http_response_status_code="401"),
    "order_403": _calls(service_name="order-service", span_name="POST /orders", span_kind="SPAN_KIND_SERVER", http_response_status_code="403"),
    "order_errors": _calls(service_name="order-service", span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"),
    "inventory_calls": _calls(service_name="inventory-service", span_kind="SPAN_KIND_SERVER"),
    "dropped_too_early": prometheus.metric_selector("otelcol_processor_tail_sampling_sampling_trace_dropped_too_early"),
    "refused_spans": prometheus.metric_selector("otelcol_receiver_refused_spans"),
}

# Gauges of the storage exporter, read over a time window instead of compared before and after:
# while writes fail, batches wait in the queue and the retrying consumers stay busy.
STORAGE_QUEUE_SIZE = prometheus.metric_selector("otelcol_exporter_queue_size", exporter="jaeger_storage_exporter")
STORAGE_QUEUE_CAPACITY = prometheus.metric_selector("otelcol_exporter_queue_capacity", exporter="jaeger_storage_exporter")


@dataclass
class Context:
    scenario: Scenario
    record: records.RunRecord
    parameters: dict
    query: jaeger.JaegerClient
    prom: prometheus.PrometheusClient
    problems: dict[str, list[str]] = field(default_factory=dict)
    subjects: dict[str, set[str]] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)

    def check(self, name: str, problems: list[str], subject: str = "") -> None:
        bucket = self.problems.setdefault(name, [])
        bucket.extend(f"{subject}: {p}" if subject else p for p in problems)
        if subject:
            self.subjects.setdefault(name, set()).add(subject)

    def note(self, name: str, subject: str) -> None:
        """A finding that does not fail the run but must stay visible in its report."""
        self.notes.setdefault(name, []).append(subject)

    def requests(self, status: int | None = None) -> list[dict]:
        return [r for r in self.record.requests if status is None or r["status"] == status]

    def trace(self, request: dict, required: set[str]) -> Trace | None:
        if not request["trace_id"]:
            return None
        return self.query.wait_for_trace(request["trace_id"], timeout=60, until=lambda t: required <= t.services)

    def metric_delta(self, key: str) -> float:
        return self.prom.total(METRICS[key]) - self.record.metrics_before.get(key, 0.0)

    def wait_metric_delta(self, key: str, expected: float, timeout: float = 120) -> float:
        deadline = time.monotonic() + timeout
        delta = self.metric_delta(key)
        while delta < expected and time.monotonic() < deadline:
            time.sleep(5)
            delta = self.metric_delta(key)
        return delta


def _stored_traces(ctx: Context, requests: list[dict], required: set[str], check_name: str) -> list[tuple[dict, Trace]]:
    found = []
    for request in requests:
        trace = ctx.trace(request, required)
        if trace is None or not required <= trace.services:
            ctx.check(check_name, ["trace not stored or incomplete"], request["trace_id"] or request["correlation_id"])
        else:
            found.append((request, trace))
    return found


KONG_GAP_NOTE = "gateway span gap (known Kong 3.9 limitation)"
KONG_GAP_DETAIL = ("{count} trace(s): Kong answered a 5xx upstream response with a new balancer span, "
                   "so OrderService's parent span id is never exported")


def _common(ctx: Context, request: dict, trace: Trace) -> None:
    ctx.check("trace integrity", tracechecks.integrity(trace), trace.trace_id)
    if tracechecks.gateway_span_gaps(trace):
        ctx.note(KONG_GAP_NOTE, trace.trace_id)
    ctx.check("kubernetes metadata", tracechecks.kubernetes_metadata(trace), trace.trace_id)
    ctx.check("correlation", tracechecks.correlation(trace, request, ctx.record.run_id), trace.trace_id)
    ctx.check("sensitive data", tracechecks.sensitive_data(trace, [f"canary-{ctx.record.run_id}"]), trace.trace_id)


def normal_order(ctx: Context) -> None:
    for request, trace in _stored_traces(ctx, ctx.requests(201), ALL_SERVICES, "traces kept"):
        _common(ctx, request, trace)
        ctx.check("topology", tracechecks.topology(trace, ALL_SERVICES, set()), trace.trace_id)
        ctx.check("stage order", tracechecks.stage_order(trace), trace.trace_id)
        ctx.check("state events", tracechecks.state_path(trace, FULL_PATH), trace.trace_id)
        ctx.check("error marking", tracechecks.error_marking(trace, technical_failure=False), trace.trace_id)


def slow_payment(ctx: Context) -> None:
    for request, trace in _stored_traces(ctx, ctx.requests(201), ALL_SERVICES, "slow traces kept without debug"):
        _common(ctx, request, trace)
        ctx.check("latency attribution", tracechecks.latency_dominance(
            trace, "payment.authorize", float(ctx.parameters.get("minShare", 0.6)), float(ctx.parameters.get("minTraceMs", 1000))), trace.trace_id)
        ctx.check("state events", tracechecks.state_path(trace, FULL_PATH), trace.trace_id)


def payment_retry(ctx: Context) -> None:
    for request, trace in _stored_traces(ctx, ctx.requests(201), ALL_SERVICES, "retried traces kept"):
        _common(ctx, request, trace)
        ctx.check("retry attempts", tracechecks.retries(trace, "payment.authorize", 2), trace.trace_id)
        ctx.check("idempotent replay", tracechecks.idempotent_replay(trace), trace.trace_id)
        ctx.check("accumulated latency", tracechecks.latency_dominance(trace, "payment.authorize", 0.5, 1500), trace.trace_id)
        ctx.check("state events", tracechecks.state_path(trace, FULL_PATH), trace.trace_id)


def _failed_flow(ctx: Context, status: int, path: list[str], required: set[str], forbidden: set[str],
                 stages: tuple[str, ...], forbidden_stages: tuple[str, ...], technical: bool, retried_stage: tuple[str, int] | None = None) -> None:
    requests = ctx.requests(status)
    if not requests:
        ctx.check("traces kept", [f"no request ended with status {status}"])
    for request, trace in _stored_traces(ctx, requests, required, "failure traces kept"):
        _common(ctx, request, trace)
        ctx.check("topology", tracechecks.topology(trace, required, forbidden), trace.trace_id)
        ctx.check("stages", tracechecks.stages_present(trace, stages, forbidden_stages), trace.trace_id)
        ctx.check("stage order", tracechecks.stage_order(trace), trace.trace_id)
        ctx.check("state events", tracechecks.state_path(trace, path), trace.trace_id)
        ctx.check("error marking", tracechecks.error_marking(trace, technical_failure=technical), trace.trace_id)
        if retried_stage:
            ctx.check("retry attempts", tracechecks.retries(trace, *retried_stage), trace.trace_id)


def payment_timeout(ctx: Context) -> None:
    _failed_flow(ctx, 504, ["InventoryReserved", "PaymentFailed"],
                 {"kong-gateway", "order-service", "inventory-service", "fraud-service", "payment-service"}, {"shipping-service"},
                 ("payment.authorize", "compensation.payment.void", "compensation.inventory.release"), ("shipping.create",),
                 technical=True, retried_stage=("payment.authorize", 3))


def inventory_failure(ctx: Context) -> None:
    _failed_flow(ctx, 502, ["InventoryFailed"],
                 {"kong-gateway", "order-service", "inventory-service", "fraud-service"}, {"payment-service", "shipping-service"},
                 ("inventory.reserve", "fraud.evaluate"), ("payment.authorize", "shipping.create"), technical=True)


def fraud_rejection(ctx: Context) -> None:
    _failed_flow(ctx, 422, ["FraudFailed"],
                 {"kong-gateway", "order-service", "inventory-service", "fraud-service"}, {"payment-service", "shipping-service"},
                 ("fraud.evaluate", "compensation.inventory.release"), ("payment.authorize", "shipping.create"), technical=False)


def shipping_failure(ctx: Context) -> None:
    _failed_flow(ctx, 502, ["InventoryReserved", "PaymentAuthorized", "ShippingFailed"],
                 ALL_SERVICES, set(),
                 ("shipping.create", "compensation.payment.void", "compensation.inventory.release"), (), technical=True)


def dependency_unavailable(ctx: Context) -> None:
    # The namespace's default-deny egress policy drops packets to a Service without endpoints, so
    # every attempt runs into its timeout instead of being refused.
    requests = ctx.requests(504)
    if not requests:
        ctx.check("missing dependency visible", ["no order failed with 504 while Shipping had no replica"])
    for request, trace in _stored_traces(ctx, requests, {"kong-gateway", "order-service", "payment-service"}, "failure traces kept"):
        _common(ctx, request, trace)
        stage = trace.named("shipping.create")
        problems = [] if stage and stage[0].attributes.get("failure.kind") == "timeout" else ["shipping.create is not marked as a timeout"]
        if trace.by_service("shipping-service", "SERVER"):
            problems.append("a ShippingService span exists although no replica was running")
        ctx.check("missing dependency visible", problems, trace.trace_id)
        ctx.check("retry attempts", tracechecks.retries(trace, "shipping.create", 3), trace.trace_id)
        ctx.check("stages", tracechecks.stages_present(trace, ("compensation.payment.void", "compensation.inventory.release")), trace.trace_id)


def _retained_by_window(ctx: Context, service: str) -> list[Trace]:
    """Traces of this run that sampling kept, found by service and the scenario run id attribute."""
    start = dt.datetime.fromtimestamp(ctx.record.workload_started_at - 5, dt.UTC)
    end = dt.datetime.fromtimestamp(ctx.record.workload_finished_at + 60, dt.UTC)
    found = ctx.query.find_traces(
        service, start, end,
        attributes={"scenario.run_id": ctx.record.run_id}, limit=1000,
    )
    return [trace for trace in found if any(s.attributes.get("scenario.run_id") == ctx.record.run_id for s in trace.spans)]


def pod_deletion(ctx: Context) -> None:
    deleted = next((e.get("pod") for e in ctx.record.kubernetes_events if e["action"] == "delete-pod"), None)
    traces = _retained_by_window(ctx, "payment-service")
    pods = {span.resource.get("k8s.pod.name") for trace in traces for span in trace.by_service("payment-service", "SERVER")}
    problems = []
    if deleted not in pods:
        problems.append(f"no kept trace shows the deleted pod {deleted} (pods seen: {sorted(p for p in pods if p)})")
    if len(pods - {deleted}) < 1:
        problems.append("no kept trace shows the replacement pod")
    ctx.check("workload attribution", problems)
    for request, trace in _stored_traces(ctx, [r for r in ctx.requests() if r["status"] in {503, 504}], {"order-service"}, "failure traces kept"):
        _common(ctx, request, trace)


def _no_new_errors(ctx: Context, name: str) -> None:
    time.sleep(METRICS_SETTLE_SECONDS)
    errors = ctx.metric_delta("order_errors")
    ctx.check(name, [] if errors == 0 else [f"{errors:.0f} OrderService error spans during the run"])


def rolling_restart(ctx: Context) -> None:
    _no_new_errors(ctx, "no errors during the rollout")
    replaced = set(next((e.get("podsBefore") or [] for e in ctx.record.kubernetes_events if e["action"] == "rollout-restart"), []))
    spans = sorted(
        (span for trace in _retained_by_window(ctx, "fraud-service") for span in trace.by_service("fraud-service", "SERVER")),
        key=lambda span: span.start_unix_nano,
    )
    # OrderService reuses pooled connections, so traffic may stay on one old pod until it stops and
    # then move to one new pod. What matters is the direction: replaced pods first, new pods last.
    pods = [span.resource.get("k8s.pod.name") for span in spans]
    problems = []
    if not pods or not replaced:
        problems.append(f"cannot compare pods: {len(pods)} kept Fraud spans, replaced pods {sorted(replaced)}")
    else:
        if pods[0] not in replaced:
            problems.append(f"the first requests were served by {pods[0]}, which is not one of the replaced pods {sorted(replaced)}")
        if pods[-1] in replaced:
            problems.append(f"the last requests were still served by the replaced pod {pods[-1]}")
    ctx.check("traffic moved from replaced to new pods", problems)


def readiness_loss(ctx: Context) -> None:
    _no_new_errors(ctx, "no errors while a replica was unready")
    unready_pod = next((pods[0] for fault in ctx.record.faults if fault["mode"] == "readiness-loss" for pods in [fault["pods"]]), None)
    cutoff = ctx.record.workload_started_at + float(ctx.parameters.get("connectionLifetimeSeconds", 35))
    late = []
    for trace in _retained_by_window(ctx, "fraud-service"):
        for span in trace.by_service("fraud-service", "SERVER"):
            if span.start_unix_nano / 1e9 > cutoff:
                late.append(span.resource.get("k8s.pod.name"))
    problems = []
    if not late:
        problems.append("no kept Fraud spans after the connection lifetime")
    elif unready_pod and unready_pod in late:
        problems.append(f"the unready pod {unready_pod} still received traffic after the connection lifetime")
    ctx.check("unready replica removed from traffic", problems)


def replica_degradation(ctx: Context) -> None:
    threshold_ms = float(ctx.parameters.get("slowSpanMs", 900))
    degraded = next((fault["pods"][0] for fault in ctx.record.faults if fault["mode"] == "latency"), None)
    slow_pods, fast_pods = set(), set()
    for trace in _retained_by_window(ctx, "fraud-service"):
        for span in trace.by_service("fraud-service", "SERVER"):
            (slow_pods if span.duration_ms >= threshold_ms else fast_pods).add(span.resource.get("k8s.pod.name"))
    problems = []
    if not slow_pods:
        problems.append(f"no Fraud span took {threshold_ms:.0f} ms or more, so the degraded pod {degraded} never served a request")
    elif slow_pods != {degraded}:
        problems.append(f"slow Fraud spans came from {sorted(p for p in slow_pods if p)}; expected only the degraded pod {degraded}")
    if not fast_pods - slow_pods:
        problems.append("no fast Fraud spans from a healthy replica")
    ctx.check("latency attributed to one replica", problems)


def _labelled(ctx: Context, label: str) -> list[dict]:
    return [request for request in ctx.requests() if request["label"] == label]


def collector_outage(ctx: Context) -> None:
    for label in ("before-outage", "after-recovery"):
        for request, trace in _stored_traces(ctx, _labelled(ctx, label), ALL_SERVICES, f"complete traces {label}"):
            _common(ctx, request, trace)
            ctx.check("state events", tracechecks.state_path(trace, FULL_PATH), trace.trace_id)
    # The services drop spans they cannot export, so the outage must leave a visible gap. Kong
    # retries its exports for a while, so its own spans may still arrive; OrderService spans may not.
    during = _labelled(ctx, "during-outage")
    survived = 0
    for request in during:
        trace = ctx.query.get_trace(request["trace_id"])
        if trace is not None and tracechecks.ORDER_SERVICE in trace.services:
            survived += 1
    problems = [] if during and not survived else [f"{survived} of {len(during)} requests from the outage window still have OrderService spans"]
    ctx.check("spans from the outage window are missing", problems)


def trace_storage_outage(ctx: Context) -> None:
    during = _labelled(ctx, "during-outage")
    if not during:
        ctx.check("traces from the outage delivered later", ["no requests were sent during the outage"])
    timeout = float(ctx.parameters.get("deliveryTimeoutSeconds", 180))
    deadline = time.monotonic() + timeout
    for request in during:
        # Retries deliver the queued spans only after OpenSearch is back, so the first trace may take a while.
        remaining = max(10.0, deadline - time.monotonic())
        trace = ctx.query.wait_for_trace(request["trace_id"], timeout=remaining, until=lambda t: ALL_SERVICES <= t.services)
        if trace is None or not ALL_SERVICES <= trace.services:
            ctx.check("traces from the outage delivered later", ["trace not complete after the storage came back"], request["trace_id"])
            continue
        ctx.check("traces from the outage delivered later", [], trace.trace_id)
        _common(ctx, request, trace)
        ctx.check("state events", tracechecks.state_path(trace, FULL_PATH), trace.trace_id)
    # The whole run plus a margin: the queue is only non-empty while writes are failing.
    window = time.time() - ctx.record.workload_started_at + 180
    peak = ctx.prom.max_over(STORAGE_QUEUE_SIZE, window)
    capacity = max((sample.value for sample in ctx.prom.query(STORAGE_QUEUE_CAPACITY)), default=0.0)
    problems = []
    if peak <= 0:
        problems.append("the exporter queue never held a batch, so the writes were never blocked")
    elif capacity and peak >= capacity:
        problems.append(f"the queue reached its capacity ({peak:.0f} of {capacity:.0f}), so batches were dropped")
    ctx.check("writes waited in a bounded queue while the storage was down", problems)
    if not problems:
        ctx.note("storage queue peak", f"{peak:.0f} of {capacity:.0f} batches")


def _edge_rejection(ctx: Context, status: int, metric: str) -> None:
    rejected = len(ctx.requests(status))
    accepted_by_kong = len([r for r in ctx.requests() if r["status"] not in {status, 0}])
    counted = ctx.wait_metric_delta(metric, rejected)
    ctx.check(f"span metrics count every {status} at Kong", [] if counted == rejected else [f"{counted:.0f} counted, {rejected} observed"])
    reached = ctx.wait_metric_delta("order_posts", accepted_by_kong, timeout=60)
    ctx.check("rejected requests never reached OrderService",
              [] if reached == accepted_by_kong else [f"OrderService handled {reached:.0f} requests; only {accepted_by_kong} passed Kong"])
    for request in ctx.requests(status)[:20]:
        if request["trace_id"]:
            trace = ctx.query.get_trace(request["trace_id"])
            if trace is not None:
                ctx.check("kept rejection traces end at Kong", [] if trace.services == {"kong-gateway"} else [f"services {sorted(trace.services)}"], trace.trace_id)


def kong_rate_limit(ctx: Context) -> None:
    _edge_rejection(ctx, 429, "kong_429")


def oversized_request(ctx: Context) -> None:
    _edge_rejection(ctx, 413, "kong_413")


def auth_denied(ctx: Context) -> None:
    for status, metric in ((401, "order_401"), (403, "order_403")):
        observed = len(ctx.requests(status))
        counted = ctx.wait_metric_delta(metric, observed)
        ctx.check(f"span metrics count every {status}", [] if counted == observed else [f"{counted:.0f} counted, {observed} observed"])
    downstream = ctx.metric_delta("inventory_calls")
    ctx.check("denied requests reached no dependency", [] if downstream == 0 else [f"{downstream:.0f} InventoryService calls"])


def traffic_spike(ctx: Context) -> None:
    rejected = len(ctx.requests(429))
    counted = ctx.wait_metric_delta("kong_429", rejected)
    ctx.check("span metrics count every 429 at Kong", [] if counted == rejected else [f"{counted:.0f} counted, {rejected} observed"])
    errors = ctx.metric_delta("order_errors")
    ctx.check("no application errors under load", [] if errors == 0 else [f"{errors:.0f} OrderService error spans"])
    dropped, refused = ctx.metric_delta("dropped_too_early"), ctx.metric_delta("refused_spans")
    ctx.check("telemetry pipeline kept up", [] if dropped == 0 and refused == 0 else [f"dropped too early={dropped:.0f}, refused={refused:.0f}"])
    now_ms = int(time.time() * 1000)
    response = ctx.query._http.get("/api/metrics/calls", params={  # noqa: SLF001 - Monitor API is outside api_v3
        "service": "kong-gateway", "endTs": now_ms, "lookback": 10 * 60_000, "step": 15_000, "ratePer": 60_000})
    points = [p for s in response.json().get("metrics", []) for p in s.get("metricPoints", [])] if response.status_code == 200 else []
    ctx.check("Monitor tab shows the spike", [] if points else [f"no call-rate data for kong-gateway ({response.status_code})"])


CONTRACTS: dict[str, Callable[[Context], None]] = {
    "normal-order": normal_order,
    "slow-payment": slow_payment,
    "payment-retry": payment_retry,
    "payment-timeout": payment_timeout,
    "inventory-failure": inventory_failure,
    "fraud-rejection": fraud_rejection,
    "shipping-failure": shipping_failure,
    "dependency-unavailable": dependency_unavailable,
    "pod-deletion": pod_deletion,
    "rolling-restart": rolling_restart,
    "readiness-loss": readiness_loss,
    "replica-degradation": replica_degradation,
    "kong-rate-limit": kong_rate_limit,
    "oversized-request": oversized_request,
    "auth-denied": auth_denied,
    "traffic-spike": traffic_spike,
    "collector-outage": collector_outage,
    "trace-storage-outage": trace_storage_outage,
}

# Contracts that compare counters take a snapshot before any traffic is sent.
METRIC_KEYS: dict[str, list[str]] = {
    "kong-rate-limit": ["kong_429", "order_posts"],
    "oversized-request": ["kong_413", "order_posts"],
    "auth-denied": ["order_401", "order_403", "inventory_calls"],
    "traffic-spike": ["kong_429", "order_errors", "dropped_too_early", "refused_spans"],
    "rolling-restart": ["order_errors"],
    "readiness-loss": ["order_errors"],
}


def snapshot_metrics(scenario: Scenario, record: records.RunRecord) -> None:
    if scenario.expect.telemetry is None:
        return
    keys = METRIC_KEYS.get(scenario.expect.telemetry.contract, [])
    if not keys:
        return
    with prometheus.connect() as prom:
        record.metrics_before = {key: prom.total(METRICS[key]) for key in keys}


def verify(scenario: Scenario, record: records.RunRecord) -> None:
    spec = scenario.expect.telemetry
    if spec is None:
        return
    if spec.contract not in CONTRACTS:
        record.add_check("telemetry contract", False, f"unknown contract '{spec.contract}'", layer="telemetry")
        return
    remaining = record.workload_finished_at + DECISION_DELAY_SECONDS - time.time()
    if remaining > 0:
        time.sleep(remaining)
    with jaeger.connect() as query, prometheus.connect() as prom:
        context = Context(scenario, record, dict(spec.parameters), query, prom)
        CONTRACTS[spec.contract](context)
    for name, problems in context.problems.items():
        unique = list(dict.fromkeys(problems))
        detail = "; ".join(unique[:6]) + (f" (+{len(unique) - 6} more)" if len(unique) > 6 else "")
        checked = len(context.subjects.get(name, ()))
        passed_detail = f"{checked} trace(s) as expected" if checked else "as expected"
        record.add_check(name, not unique, detail or passed_detail, layer="telemetry")
    for name, subjects in context.notes.items():
        unique = list(dict.fromkeys(subjects))
        detail = KONG_GAP_DETAIL.format(count=len(unique)) if name == KONG_GAP_NOTE else "; ".join(unique[:3])
        record.add_check(name, True, detail, layer="telemetry")
    if not context.problems:
        record.add_check("telemetry contract", False, "the contract produced no checks", layer="telemetry")
