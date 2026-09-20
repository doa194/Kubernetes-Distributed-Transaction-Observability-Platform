"""Telemetry-pipeline suite: proves the Collector's processing rules with controlled telemetry.

Synthetic spans (service `pipeline-probe`) are sent once, with exactly chosen status, duration and
attributes; after the tail-sampling decision window every check inspects what was stored in
Jaeger and counted in Prometheus. A few checks use real gateway traffic where the behavior depends
on what Kong actually emits.

Probe span names never contain run ids, so repeated runs do not create new metric series. Counters
are compared with a snapshot taken before sending; the experiment lock held by `platformctl
validate` guarantees no other traffic changes them meanwhile.
"""

from __future__ import annotations

import ipaddress
import time
import uuid

import httpx

from txplatform import identity, jaeger, orders, otlp, prometheus, stats
from txplatform.validation.framework import Suite, expect

# Must stay above the Collector's tail-sampling decision_wait plus export and index delays.
DECISION_DELAY_SECONDS = 35
BASELINE_TRACES = 400
BASELINE_PROBABILITY = 0.25
# Per-request identifiers and per-pod resource attributes: either would create unbounded metric series.
FORBIDDEN_METRIC_LABELS = {
    "order_id", "scenario_id", "scenario_run_id", "correlation_id", "kong_request_id", "trace_id", "span_id",
    "exported_instance", "service_instance_id", "k8s_pod_name", "collector_instance_id",
}
CLIENT_ADDRESS_KEYS = ("http.client_ip", "net.peer.ip", "client.address", "network.peer.address")

COUNTERS = {
    "baseline_calls": prometheus.metric_selector("traces_span_metrics_calls", service_name=otlp.PROBE_SERVICE, span_name="probe.baseline"),
    "health_calls": prometheus.metric_selector("traces_span_metrics_calls", service_name=otlp.PROBE_SERVICE, span_name="GET /health/ready"),
    "kong_401": prometheus.metric_selector("traces_span_metrics_calls", service_name="kong-gateway", http_response_status_code="401"),
    "dropped_too_early": prometheus.metric_selector("otelcol_processor_tail_sampling_sampling_trace_dropped_too_early"),
    "refused": prometheus.metric_selector("otelcol_receiver_refused_spans"),
    "export_failed": prometheus.metric_selector("otelcol_exporter_send_failed_spans"),
    "errors_policy_sampled": prometheus.metric_selector("otelcol_processor_tail_sampling_count_traces_sampled", policy="errors", sampled="true"),
}


def _looks_like_ip(value: object) -> bool:
    try:
        ipaddress.ip_address(str(value))
        return True
    except ValueError:
        return False


def build() -> Suite:
    suite = Suite("telemetry-pipeline", "tail sampling, unsampled span metrics, safe filtering, sanitation, cardinality, SPM, self-metrics")
    tokens = identity.TokenProvider()
    run = uuid.uuid4().hex[:8]
    spans: dict[str, list[otlp.SyntheticSpan]] = {}
    real: dict[str, str] = {}
    before: dict[str, float] = {}
    sent_at: list[float] = []

    def delta(prom: prometheus.PrometheusClient, key: str) -> float:
        return prom.total(COUNTERS[key]) - before[key]

    def wait_for_delta(key: str, expected: float, timeout: float = 90) -> float:
        """Counters reach Prometheus with the next span-metrics flush and scrape (15 s each)."""
        deadline = time.monotonic() + timeout
        with prometheus.connect() as prom:
            value = delta(prom, key)
            while value < expected and time.monotonic() < deadline:
                time.sleep(5)
                value = delta(prom, key)
        return value

    def prepare() -> None:
        if sent_at:
            return
        with prometheus.connect() as prom:
            before.update({key: prom.total(selector) for key, selector in COUNTERS.items()})
        spans["errors"] = [otlp.SyntheticSpan("probe.error", error=True) for _ in range(5)]
        spans["slow"] = [otlp.SyntheticSpan("probe.slow", duration_ms=1500) for _ in range(5)]
        spans["debug"] = [otlp.SyntheticSpan("probe.debug", attributes={"sampling.debug": True}) for _ in range(5)]
        spans["baseline"] = [otlp.SyntheticSpan("probe.baseline") for _ in range(BASELINE_TRACES)]
        # A health probe span without parent must be dropped entirely (the debug flag would
        # otherwise force it into storage)...
        spans["health_root"] = [otlp.SyntheticSpan("GET /health/ready", attributes={"url.path": "/health/ready", "sampling.debug": True})]
        # ...but a span inside a real trace must never be removed, or the trace would break.
        parent = otlp.SyntheticSpan("probe.parent", attributes={"sampling.debug": True})
        child = otlp.SyntheticSpan("GET /health/live", trace_id=parent.trace_id, parent_span_id=parent.span_id, attributes={"url.path": "/health/live"}, kind=3)
        spans["health_child"] = [parent, child]
        spans["sensitive"] = [otlp.SyntheticSpan("probe.sensitive", attributes={
            "sampling.debug": True,
            "http.request.header.authorization": f"Bearer canary-auth-{run}",
            "user.password": f"canary-password-{run}",
            "payment.card_number": "4111111111111111",
            "http.url": f"https://shop.test/orders?access_token=canary-url-{run}",
            "order.id": f"order-{run}",
        })]
        spans["late"] = [otlp.SyntheticSpan("probe.late", attributes={"sampling.debug": True})]
        with otlp.connect() as sender:
            sender.send([s for group in spans.values() for s in group])
        # Real traffic: an order through Kong (its root span carries the client address) and an
        # unauthenticated request whose 401 Kong records in its legacy status attribute.
        real["trace_id"] = orders.trace_id_of(orders.place(tokens, debug=True))
        with identity.https_client(identity.GATEWAY_URL) as gateway:
            gateway.post("/orders", json=orders.normal_order())
        sent_at.append(time.monotonic())

    def after_decision() -> None:
        prepare()
        remaining = sent_at[0] + DECISION_DELAY_SECONDS - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def stored(query: jaeger.JaegerClient, group: str) -> list[bool]:
        return [query.get_trace(span.trace_id) is not None for span in spans[group]]

    @suite.check("error, slow and debug traces are always kept")
    def policy_retention() -> str:
        after_decision()
        with jaeger.connect() as query:
            results = {group: stored(query, group) for group in ("errors", "slow", "debug")}
        missing = {group: kept.count(False) for group, kept in results.items() if not all(kept)}
        expect(not missing, f"traces dropped despite their policy: {missing}")
        return "5/5 error, 5/5 slow, 5/5 debug traces stored"

    @suite.check("normal traces are kept at the baseline rate")
    def baseline_rate() -> str:
        after_decision()
        with jaeger.connect() as query:
            kept = sum(stored(query, "baseline"))
        band = stats.binomial_band(BASELINE_TRACES, BASELINE_PROBABILITY)
        expect(band.contains(kept), f"{kept} of {BASELINE_TRACES} kept; expected {band.low}-{band.high}")
        return f"{kept} of {BASELINE_TRACES} kept (expected about {band.expected:.0f})"

    @suite.check("span metrics count every span, including traces that sampling dropped")
    def unsampled_metrics() -> str:
        after_decision()
        counted = wait_for_delta("baseline_calls", BASELINE_TRACES)
        expect(counted == BASELINE_TRACES, f"span metrics counted {counted:.0f} calls for {BASELINE_TRACES} sent spans")
        return f"{counted:.0f} calls counted for {BASELINE_TRACES} spans, most of them not stored"

    @suite.check("late spans join traces that were already kept")
    def late_spans() -> str:
        after_decision()
        root = spans["late"][0]
        late_child = otlp.SyntheticSpan("probe.late.child", trace_id=root.trace_id, parent_span_id=root.span_id)
        with otlp.connect() as sender:
            sender.send([late_child])
        with jaeger.connect() as query:
            trace = query.wait_for_trace(root.trace_id, timeout=45, until=lambda t: len(t.spans) == 2)
        expect(trace is not None and len(trace.spans) == 2, "the late child span was not stored with its kept trace")
        return "late child stored next to its root"

    @suite.check("health spans are filtered without breaking traces")
    def safe_filtering() -> str:
        after_decision()
        with jaeger.connect() as query:
            health_root_stored = query.get_trace(spans["health_root"][0].trace_id) is not None
            parent_trace = query.get_trace(spans["health_child"][0].trace_id)
        expect(not health_root_stored, "a parentless health span reached trace storage")
        expect(parent_trace is not None and len(parent_trace.spans) == 2, "a child span was filtered out of its trace")
        with prometheus.connect() as prom:
            counted = delta(prom, "health_calls")
        expect(counted == 0, f"filtered health spans were still counted ({counted:.0f})")
        return "health root dropped everywhere; child span kept inside its trace"

    @suite.check("sensitive attributes are removed and client addresses hashed")
    def sanitation() -> str:
        after_decision()
        with jaeger.connect() as query:
            synthetic = query.get_trace(spans["sensitive"][0].trace_id)
            real_trace = query.wait_for_trace(real["trace_id"], timeout=30)
        expect(synthetic is not None, "sensitive probe trace not stored")
        attributes = synthetic.spans[0].attributes
        leaked = [key for key in ("http.request.header.authorization", "user.password", "payment.card_number", "http.url") if key in attributes]
        expect(not leaked, f"sensitive attributes stored: {leaked}")
        expect(attributes.get("url.path") == "/orders", f"the URL path was not kept: {attributes.get('url.path')}")
        expect(attributes.get("order.id") == f"order-{run}", "a harmless attribute was removed")
        expect(real_trace is not None, "real gateway trace not stored")
        kong_root = next(span for span in real_trace.roots() if span.service == "kong-gateway")
        addresses = {key: kong_root.attributes[key] for key in CLIENT_ADDRESS_KEYS if key in kong_root.attributes}
        raw = [key for key, value in addresses.items() if _looks_like_ip(value)]
        expect(not raw, f"client addresses stored in clear text: {raw}")
        return f"secrets and query string removed, path kept; Kong client address attributes hashed: {sorted(addresses) or 'none recorded'}"

    @suite.check("Kong's legacy HTTP status becomes the standard metric dimension")
    def normalization() -> str:
        after_decision()
        counted = wait_for_delta("kong_401", 1, timeout=60)
        expect(counted >= 1, "no new kong-gateway span metrics with http_response_status_code=401")
        return f"{counted:.0f} new Kong calls with status 401 in span metrics"

    @suite.check("span metrics carry no high-cardinality identifiers")
    def cardinality() -> str:
        after_decision()
        with prometheus.connect() as prom:
            labels = prom.current_label_names('{__name__=~"traces_span_metrics_.*"}')
            overflow = prom.total('{__name__=~"traces_span_metrics_calls(_total)?",otel_metric_overflow="true"}')
        forbidden = labels & FORBIDDEN_METRIC_LABELS
        expect(not forbidden, f"identifier labels on span metrics: {sorted(forbidden)}")
        expect(overflow == 0, "the span metrics cardinality limit was reached")
        return f"labels: {sorted(labels)}"

    @suite.check("Jaeger's Monitor API returns RED metrics for real services")
    def spm_api() -> str:
        after_decision()
        params = {"service": "order-service", "lookback": 15 * 60_000, "step": 60_000, "ratePer": 60_000}
        with jaeger.connect() as query:
            http: httpx.Client = query._http  # noqa: SLF001 - the Monitor API is outside api_v3
            # A rate needs at least two scrapes of a series, so freshly created series take a moment.
            deadline = time.monotonic() + 90
            while True:
                params["endTs"] = int(time.time() * 1000)
                calls = http.get("/api/metrics/calls", params=params).raise_for_status().json()
                points = [p for series in calls.get("metrics", []) for p in series.get("metricPoints", [])]
                if points or time.monotonic() > deadline:
                    break
                time.sleep(10)
            errors = http.get("/api/metrics/errors", params=params)
            latencies = http.get("/api/metrics/latencies", params={**params, "quantile": 0.95})
        expect(points, f"no call-rate data for order-service: {calls}")
        expect(errors.status_code == 200 and latencies.status_code == 200, f"errors={errors.status_code}, latencies={latencies.status_code}")
        return f"{len(calls['metrics'])} call-rate series, errors and latencies available"

    @suite.check("Collector self-metrics show healthy sampling and ingestion")
    def self_metrics() -> str:
        after_decision()
        sampled_errors = wait_for_delta("errors_policy_sampled", 5, timeout=60)
        with prometheus.connect() as prom:
            dropped_early, refused, export_failed = (delta(prom, key) for key in ("dropped_too_early", "refused", "export_failed"))
        expect(dropped_early == 0, f"{dropped_early:.0f} traces were evicted before a sampling decision")
        expect(refused == 0 and export_failed == 0, f"refused={refused:.0f}, export failures={export_failed:.0f}")
        expect(sampled_errors >= 5, f"the errors policy sampled {sampled_errors:.0f} of the 5 error traces")
        return f"no early drops, refusals or export failures; errors policy sampled {sampled_errors:.0f} traces"

    return suite
