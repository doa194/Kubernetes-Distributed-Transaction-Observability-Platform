{{/*
Jaeger configurations as named templates, so each is rendered once into its ConfigMap and once
into the pod's checksum annotation (a changed configuration restarts the pod).
*/}}

{{/*
OpenSearch trace storage, shared by the Collector (writes) and Query (reads).
Query additionally reads span metrics from Prometheus for the Monitor tab.
*/}}
{{- define "observability.storage" -}}
jaeger_storage:
  backends:
    trace_storage:
      opensearch:
        server_urls:
          - {{ .root.Values.openSearchUrl }}
        indices:
          index_prefix: {{ .root.Values.indexPrefix }}
          # One index per day and no replicas: the retention job deletes whole days, and a
          # single-node cluster cannot place replica shards anyway.
          spans: { date_layout: "2006-01-02", rollover_frequency: day, shards: 1, replicas: 0 }
          services: { date_layout: "2006-01-02", rollover_frequency: day, shards: 1, replicas: 0 }
          dependencies: { date_layout: "2006-01-02", rollover_frequency: day, shards: 1, replicas: 0 }
          sampling: { date_layout: "2006-01-02", rollover_frequency: day, shards: 1, replicas: 0 }
        # Each batch is written with one blocking bulk request, so a failed write is reported to the
        # exporter (and retried) instead of being lost silently in a client-side buffer.
        write_mode: sync
  {{- if .withMetrics }}
  metric_backends:
    span_metrics:
      prometheus:
        endpoint: {{ .root.Values.prometheusUrl }}
        # The span metrics carry the "_total" and unit suffixes added by the Prometheus exporter.
        normalize_calls: true
        normalize_duration: true
  {{- end }}
{{- end }}

{{/* Internal telemetry and health endpoints shared by both roles. */}}
{{- define "observability.telemetry" -}}
telemetry:
  resource:
    attributes:
      - name: service.name
        value: {{ .role }}
  metrics:
    level: detailed
    readers:
      - pull:
          exporter:
            prometheus:
              host: 0.0.0.0
              port: 8888
  logs:
    level: info
{{- end }}

{{/*
Collector pipelines:

  otlp -> traces/intake -> span_metrics --------> metrics/spanmetrics -> prometheus (:8889)
                        \-> forward/sampling ---> traces/storage (tail sampling) -> OpenSearch

Every span is cleaned once in traces/intake. Span metrics are computed from all spans before
sampling, so request rates and error counts stay exact even though most traces are not stored.
*/}}
{{- define "observability.collectorConfig" -}}
{{- $sampling := .Values.collector.sampling -}}
service:
  extensions: [jaeger_storage, healthcheckv2]
  pipelines:
    traces/intake:
      receivers: [otlp]
      processors: [memory_limiter, filter/noise, attributes/normalize, attributes/sanitize, attributes/client-address]
      exporters: [span_metrics, forward/sampling]
    traces/storage:
      receivers: [forward/sampling]
      processors: [tail_sampling, batch]
      exporters: [jaeger_storage_exporter]
    metrics/spanmetrics:
      receivers: [span_metrics]
      exporters: [prometheus]
  {{- include "observability.telemetry" (dict "role" "jaeger-collector") | nindent 2 }}
extensions:
  healthcheckv2:
    use_v2: true
    http:
      endpoint: 0.0.0.0:13133
  {{- include "observability.storage" (dict "root" . "withMetrics" false) | nindent 2 }}
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
processors:
  # First in the pipeline: refuses new data before the process runs out of memory.
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 20

  # Health and metrics probes carry no transaction information. Only spans without a parent are
  # dropped: removing a span from inside a trace would break the trace's parent-child tree.
  filter/noise:
    error_mode: ignore
    trace_conditions:
      # IsRootSpan() only exists in the span context, so the context is named explicitly.
      - context: span
        conditions:
          - IsRootSpan() and IsMatch(span.attributes["url.path"], "^/(health|metrics)(/|$)")

  # Kong still uses older attribute names. Copying them to the current names means metrics and
  # searches use one name for every service.
  attributes/normalize:
    actions:
      - { key: http.response.status_code, from_attribute: http.status_code, action: insert }
      - { key: http.request.method, from_attribute: http.method, action: insert }
      - { key: http.status_code, action: delete }
      - { key: http.method, action: delete }

  # Secrets must never be stored, whichever instrumentation recorded them. Attribute names that
  # suggest credentials or card data are deleted. Full URLs may carry credentials in their query
  # string, so only their path is kept (as url.path).
  attributes/sanitize:
    actions:
      - pattern: (?i)(authorization|cookie|password|passwd|secret|token|api[._-]?key|card[._-]?number)
        action: delete
      - key: http.url
        pattern: ^(?:[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*)?(?P<sanitized_url_path>[^?#]*)
        action: extract
      - key: url.full
        pattern: ^(?:[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*)?(?P<sanitized_url_path>[^?#]*)
        action: extract
      - { key: url.path, from_attribute: sanitized_url_path, action: insert }
      - { key: sanitized_url_path, action: delete }
      - { key: http.url, action: delete }
      - { key: url.full, action: delete }
      - { key: url.query, action: delete }

  # Kong's request span records the caller's IP address. It is replaced by its SHA-256 hash: the
  # same caller still produces the same value, but the address itself is not stored.
  attributes/client-address:
    include:
      match_type: strict
      services: [kong-gateway]
      span_names: [kong]
    actions:
      - { key: http.client_ip, action: hash }
      - { key: net.peer.ip, action: hash }

  # Decides per trace, after waiting for its spans, whether it is stored. Any matching policy keeps
  # the trace. The decision caches remember recent decisions, so spans that arrive late follow the
  # decision already made for their trace instead of creating a partial trace.
  tail_sampling:
    decision_wait: {{ $sampling.decisionWait }}
    num_traces: {{ $sampling.maxTracesInMemory }}
    expected_new_traces_per_sec: {{ $sampling.expectedNewTracesPerSecond }}
    decision_cache:
      sampled_cache_size: {{ $sampling.decisionCacheSize }}
      non_sampled_cache_size: {{ $sampling.decisionCacheSize }}
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: slow
        type: latency
        latency: { threshold_ms: {{ $sampling.slowThresholdMs }} }
      - name: debug
        type: boolean_attribute
        boolean_attribute: { key: sampling.debug, value: true }
      - name: baseline
        type: probabilistic
        probabilistic: { sampling_percentage: {{ $sampling.baselinePercentage }} }
  batch: {}
connectors:
  forward/sampling: {}
  # Request rate, errors and duration per service and operation (RED metrics).
  span_metrics:
    namespace: traces.span.metrics
    metrics_flush_interval: 15s
    # Only low-cardinality dimensions: identifiers such as order ids stay on spans.
    dimensions:
      - name: http.response.status_code
    # One series per service, not per pod: resource attributes such as the pod name or instance id
    # would start new series after every restart. (The Collector also runs with the
    # connector.spanmetrics.excludeResourceMetrics feature gate, which keeps them off the metrics.)
    resource_metrics_key_attributes: [service.name]
    # There is only one Collector, so its instance id would only start new series after a restart.
    exclude_dimensions: [collector.instance.id]
    # Safety net: new dimension combinations beyond this limit are counted as one overflow series.
    aggregation_cardinality_limit: {{ .Values.collector.spanMetrics.cardinalityLimit }}
    histogram:
      explicit:
        buckets: [5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2s, 5s, 10s, 15s]
exporters:
  jaeger_storage_exporter:
    trace_storage: trace_storage
    # Failed writes are retried with growing pauses for up to five minutes, so a short storage
    # outage delays traces instead of losing them.
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 300s
    # A bounded queue: during a longer outage new batches are dropped (and counted) once it is
    # full, instead of growing without limit until the Collector runs out of memory.
    queue:
      num_consumers: 4
      queue_size: 200
  prometheus:
    endpoint: 0.0.0.0:8889
{{- end }}

{{- define "observability.queryConfig" -}}
service:
  extensions: [jaeger_storage, jaeger_query, healthcheckv2]
  # The query role receives no spans; the collector framework still requires one pipeline.
  pipelines:
    traces:
      receivers: [nop]
      processors: [batch]
      exporters: [nop]
  {{- include "observability.telemetry" (dict "role" "jaeger-query") | nindent 2 }}
extensions:
  healthcheckv2:
    use_v2: true
    http:
      endpoint: 0.0.0.0:13133
  jaeger_query:
    storage:
      traces: trace_storage
      metrics: span_metrics
    ui:
      config_file: /etc/jaeger/ui-config.json
    http:
      endpoint: 0.0.0.0:16686
    grpc:
      endpoint: 0.0.0.0:16685
  {{- include "observability.storage" (dict "root" . "withMetrics" true) | nindent 2 }}
receivers:
  nop: {}
processors:
  batch: {}
exporters:
  nop: {}
{{- end }}

{{- define "observability.uiConfig" -}}
{
  "criticalPathEnabled": true,
  "archiveEnabled": false,
  "dependencies": { "menuEnabled": false },
  "monitor": { "menuEnabled": true }
}
{{- end }}
