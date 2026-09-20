# Metrics and monitoring

Traces answer *"what happened to this request?"*. Metrics answer *"how many, how fast, and how many
failed?"* — including for requests whose traces were never stored. This document describes which
metrics exist, how they are derived from spans, how to query them, and how the experiments use them
as evidence.

---

## Where the metrics come from

The platform does not instrument metrics separately. Every metric is either derived from spans or
reported by the telemetry components about themselves:

| Kind | Produced by | Examples | Used for |
| --- | --- | --- | --- |
| **Span metrics** | The Collector, from every span it receives | `traces_span_metrics_calls_total`, `traces_span_metrics_duration_milliseconds_bucket` | Request rate, errors and latency per service and operation |
| **Self-metrics** | The Collector and Jaeger Query, about themselves | `otelcol_receiver_accepted_spans`, `otelcol_exporter_queue_size` | Health of the telemetry pipeline |

Deriving metrics from spans means one instrumentation serves both purposes: a service that is traced
is automatically measured, with the same names and the same labels.

Request rate, errors and duration together are often called **RED metrics** (Rate, Errors,
Duration) — the three numbers that describe how a request-serving component is doing.

## Prometheus

Prometheus stores the metrics. Its role is deliberately narrow — it is not a general cluster
monitoring stack:

| Setting | Value |
| --- | --- |
| Scrape interval | 15 seconds |
| Retention | 2 days, on a 2 GiB volume |
| Optional components | Alertmanager, node exporter, kube-state-metrics and Pushgateway are disabled |
| Target discovery | Four fixed targets; no Kubernetes service discovery and therefore no access to the Kubernetes API |
| Access | Cluster-internal only; `uv run platformctl ui prometheus` opens it at `http://127.0.0.1:9090` |

| Scrape job | Target | Contents |
| --- | --- | --- |
| `span-metrics` | Collector, port 8889 | The span metrics |
| `jaeger-collector` | Collector, port 8888 | Collector self-metrics |
| `jaeger-query` | Query, port 8888 | Query self-metrics |
| `prometheus` | itself | Prometheus's own health |

## Span metrics

For every span, the Collector's span-metrics connector increments a call counter and records the
duration in a histogram. It publishes the result every 15 seconds.

| Metric | Type | Meaning |
| --- | --- | --- |
| `traces_span_metrics_calls_total` | Counter | Number of spans |
| `traces_span_metrics_duration_milliseconds_bucket` (`_sum`, `_count`) | Histogram | Span durations in buckets of 5, 10, 25, 50, 100, 250, 500 ms, 1, 2, 5, 10 and 15 s |

A real series from the running platform:

```text
traces_span_metrics_calls_total{
  service_name="order-service", span_name="POST /orders", span_kind="SPAN_KIND_SERVER",
  status_code="STATUS_CODE_UNSET", http_response_status_code="201",
  job="span-metrics", instance="jaeger-collector.observability.svc.cluster.local:8889" }
```

| Label | Meaning |
| --- | --- |
| `service_name` | The service that produced the span (`kong-gateway`, `order-service`, …) |
| `span_name` | The operation (`kong`, `POST /orders`, `payment.authorize`, …) |
| `span_kind` | `SPAN_KIND_SERVER` for incoming requests, `SPAN_KIND_CLIENT` for outgoing calls, `SPAN_KIND_INTERNAL` for business steps |
| `status_code` | `STATUS_CODE_ERROR` for technical failures; business rejections stay `STATUS_CODE_UNSET` ([tracing.md](tracing.md)) |
| `http_response_status_code` | The HTTP status, where the span has one |

### Exact, even when the trace is not stored

The span metrics are computed **before** tail sampling ([telemetry-pipeline.md](telemetry-pipeline.md)).
A request whose trace is dropped, or which Kong rejected before it reached any service, still
increments its counter. This is what makes the metrics trustworthy for counting — and it is used as
evidence by several experiments (see below).

### Controlled cardinality

**Cardinality** is the number of distinct label combinations, and therefore of separate time series.
Uncontrolled, it is the most common way to overload a metrics system. Four rules keep it bounded:

| Rule | Effect |
| --- | --- |
| Identifiers stay on spans | Order ids, correlation ids, trace ids and scenario ids are never metric labels — each would create a new series per request |
| One series per service, not per pod | Pod names and instance ids are excluded (the Collector runs with the `connector.spanmetrics.excludeResourceMetrics` feature gate), so a restart or rollout does not start new series |
| Only one extra label | `http_response_status_code` is the only label added to the connector's defaults |
| A hard limit | Beyond 2 000 label combinations, new combinations are merged into one overflow series (labelled `otel_metric_overflow="true"`) |

The telemetry-pipeline suite reads the label names of the live series and fails if any identifier or
per-pod label appears, or if the overflow series exists.

## Querying the metrics

Open Prometheus with `uv run platformctl ui prometheus` and enter a query at
`http://127.0.0.1:9090/graph`. Rates need at least two scrapes, so a new series appears about half a
minute after its first request.

```promql
# Requests per second per service (incoming requests only)
sum by (service_name) (
  rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[5m]))

# Share of OrderService requests that failed technically
sum(rate(traces_span_metrics_calls_total{service_name="order-service", span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m]))
/
sum(rate(traces_span_metrics_calls_total{service_name="order-service", span_kind="SPAN_KIND_SERVER"}[5m]))

# 95th-percentile latency per service, in milliseconds
histogram_quantile(0.95, sum by (le, service_name) (
  rate(traces_span_metrics_duration_milliseconds_bucket{span_kind="SPAN_KIND_SERVER"}[5m])))

# Requests Kong rejected itself (too large or too many) in the last 15 minutes
sum by (http_response_status_code) (
  increase(traces_span_metrics_calls_total{service_name="kong-gateway", span_name="kong", http_response_status_code=~"413|429"}[15m]))
```

## Jaeger's Monitor tab

Jaeger Query reads the span metrics back from Prometheus and shows them in its **Monitor** tab:
request rate, error rate and 95th-percentile latency for each service and operation, next to the
traces themselves — no separate dashboard tool is needed.

```bash
uv run platformctl ui jaeger   # then open http://127.0.0.1:16686 and select the Monitor tab
```

The tab uses an HTTP API that can also be called directly (times in milliseconds):

```text
GET /api/metrics/calls?service=order-service&endTs=<now>&lookback=900000&step=60000&ratePer=60000
GET /api/metrics/errors?service=order-service&endTs=<now>&lookback=900000&step=60000&ratePer=60000
GET /api/metrics/latencies?service=order-service&quantile=0.95&endTs=<now>&lookback=900000&step=60000&ratePer=60000
```

## Metrics as experimental evidence

The experiment runner reads counters before and after each run and compares the difference with what
the scenario did. Because only one experiment runs at a time ([experiment-safety.md](experiment-safety.md)),
the difference is exact.

| Scenario | Counter | Measured |
| --- | --- | --- |
| `kong-rate-limit` | `kong` spans with status 429 | All 76 rejected requests counted; OrderService received none of them |
| `oversized-request` | `kong` spans with status 413 | All 5 counted; OrderService received none of them |
| `auth-denied` | OrderService `POST /orders` with status 401 and 403; InventoryService server spans | 10 × 401 and 5 × 403 counted; no dependency call |
| `rolling-restart`, `readiness-loss` | OrderService server spans with an error status | None while pods were replaced or unready |
| `traffic-spike` | 429s at Kong; OrderService errors; Collector refusals and early drops | 180 rejections counted, no application errors, no telemetry loss; the Monitor API shows the spike |
| `trace-storage-outage` | The storage exporter's queue size | Peaked at 12 of 200 batches |

## Self-metrics

The Collector's own metrics show whether the pipeline itself is healthy — whether spans are refused,
writes fail, or traces are dropped before their sampling decision. The metrics worth watching and
their meaning are listed in [telemetry-pipeline.md](telemetry-pipeline.md#the-pipeline-watches-itself).
The `observability` and `telemetry-pipeline` suites check them after controlled traffic, and the
`traffic-spike` and `trace-storage-outage` scenarios check them under load and during an outage.

## What is not monitored

| Not included | Why |
| --- | --- |
| Infrastructure metrics (node CPU and memory, container restarts, disk) | The platform's subject is transaction observability; a cluster monitoring stack would add many components without making that subject clearer |
| Alerting | No one is on call for a local environment; the scenario checks play the role of assertions instead |
| Custom business metrics (orders per minute by state, revenue) | Span metrics already count every order by outcome; business dashboards would be the next step |

The production counterparts are listed in
[production-considerations.md](production-considerations.md).

## Related documents

- [Telemetry pipeline](telemetry-pipeline.md) — where span metrics are computed, and the self-metrics
- [Sampling](sampling.md) — why stored traces are incomplete while metrics are not
- [Walkthrough](walkthrough.md) — the Monitor tab and first queries, step by step
- [Scenario catalog](scenario-catalog.md) — the experiments that use metrics as evidence
