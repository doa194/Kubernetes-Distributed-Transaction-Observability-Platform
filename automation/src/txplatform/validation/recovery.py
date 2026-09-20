"""Recovery suite (disruptive): the platform stays stable when it is redeployed or overloaded.

- Redeploying every component again must not replace or restart any pod that is already correct,
  so an operator can always re-run a deployment safely.
- More new traces than the Collector's sampling buffer can hold must be dropped in a bounded,
  visible way (a self-metric counts them) without the Collector crashing, and ingestion must
  continue afterwards.
"""

from __future__ import annotations

import time

import yaml

from txplatform import jaeger, kube, lifecycle, otlp, prometheus
from txplatform.validation.framework import Suite, expect

DECISION_DELAY_SECONDS = 35
DROPPED_TOO_EARLY = prometheus.metric_selector("otelcol_processor_tail_sampling_sampling_trace_dropped_too_early")


def _pod_snapshot() -> dict[str, tuple[str, int]]:
    """Pod name -> (uid, total container restarts) for every long-running platform pod."""
    snapshot = {}
    for namespace in kube.PLATFORM_NAMESPACES:
        for pod in kube.clients().core.list_namespaced_pod(namespace).items:
            labels = pod.metadata.labels or {}
            if labels.get("txplatform.io/probe") or "job-name" in labels or pod.status.phase != "Running":
                continue
            restarts = sum(status.restart_count for status in pod.status.container_statuses or [])
            snapshot[f"{namespace}/{pod.metadata.name}"] = (pod.metadata.uid, restarts)
    return snapshot


def _collector_buffer_size() -> int:
    config_map = kube.clients().core.read_namespaced_config_map("jaeger-collector-config", kube.NS_OBSERVABILITY)
    return int(yaml.safe_load(config_map.data["config.yaml"])["processors"]["tail_sampling"]["num_traces"])


def build() -> Suite:
    suite = Suite("recovery", "idempotent redeployment, bounded Collector overload")

    @suite.check("redeploying every component replaces or restarts no healthy pod")
    def idempotent_redeploy() -> str:
        before = _pod_snapshot()
        lifecycle.deploy(lifecycle.component_names())
        after = _pod_snapshot()
        replaced = sorted(set(before) ^ set(after))
        restarted = sorted(name for name in set(before) & set(after) if before[name] != after[name])
        expect(not replaced and not restarted, f"replaced pods: {replaced}; restarted pods: {restarted}")
        return f"{len(after)} pods unchanged after deploying {len(lifecycle.component_names())} components again"

    @suite.check("an overflow of the Collector's sampling buffer is bounded, visible and survived")
    def sampling_buffer_overflow() -> str:
        buffer_size = _collector_buffer_size()
        collector_before = {name: state for name, state in _pod_snapshot().items() if "/jaeger-collector-" in name}
        with prometheus.connect() as prom:
            dropped_before = prom.total(DROPPED_TOO_EARLY)

        # A quarter more new traces than the buffer holds, all within one decision window.
        overflow = [otlp.SyntheticSpan("probe.overflow") for _ in range(buffer_size + buffer_size // 4)]
        with otlp.connect() as sender:
            sender.send(overflow, batch_size=2000)

        dropped = 0.0
        with prometheus.connect() as prom:
            deadline = time.monotonic() + 90
            while dropped == 0 and time.monotonic() < deadline:
                time.sleep(5)
                dropped = prom.total(DROPPED_TOO_EARLY) - dropped_before
        expect(dropped > 0, "no trace was reported as dropped although the buffer overflowed")

        collector_after = {name: state for name, state in _pod_snapshot().items() if "/jaeger-collector-" in name}
        expect(collector_after == collector_before, f"the Collector was restarted or replaced: {collector_before} -> {collector_after}")

        # Ingestion continues: a flagged trace sent after the overflow is stored as usual.
        marker = otlp.SyntheticSpan("probe.after-overflow", attributes={"sampling.debug": True})
        with otlp.connect() as sender:
            sender.send([marker])
        with jaeger.connect() as query:
            stored = query.wait_for_trace(marker.trace_id, timeout=DECISION_DELAY_SECONDS + 60)
        expect(stored is not None, "a trace sent after the overflow was not stored")
        return f"{len(overflow)} traces for a buffer of {buffer_size}: {dropped:.0f} dropped before their decision, Collector kept running"

    return suite
