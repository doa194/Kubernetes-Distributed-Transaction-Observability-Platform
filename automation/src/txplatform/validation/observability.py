"""Observability suite: the split Jaeger v2 platform stores, serves and reports on traces.

Non-disruptive checks only. Restarts and outages of the telemetry components are covered by the
separate `observability-resilience` suite.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid

import httpx
from kubernetes import client

from txplatform import identity, jaeger, kube, orders, portforward, probes, prometheus
from txplatform.validation.framework import Suite, expect

INDEX_PREFIX = "txplatform"


def opensearch_client(port: int) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30)


def build() -> Suite:
    suite = Suite("observability", "OpenSearch storage, split Jaeger roles, Prometheus self-monitoring, retention, policies")
    tokens = identity.TokenProvider()
    state: dict[str, str] = {}

    @suite.check("OpenSearch, Collector, Query and Prometheus are ready")
    def components_ready() -> str:
        expect(kube.statefulset_ready(kube.NS_OBSERVABILITY, "opensearch-traces"), "OpenSearch is not ready")
        for deployment in ("jaeger-collector", "jaeger-query", "prometheus-server"):
            expect(kube.deployment_available(kube.NS_OBSERVABILITY, deployment), f"{deployment} is not available")
        with portforward.forward(kube.NS_OBSERVABILITY, "svc/opensearch-traces", 9200) as port, opensearch_client(port) as search:
            health = search.get("/_cluster/health").raise_for_status().json()
        expect(health["status"] == "green", f"OpenSearch cluster status is {health['status']}")
        return "all ready, OpenSearch green"

    @suite.check("a gateway order is stored in OpenSearch and served by Jaeger Query")
    def stored_and_served() -> str:
        response = orders.place(tokens, debug=True)
        expect(response.status_code == 201, f"order returned {response.status_code}")
        trace_id = orders.trace_id_of(response)
        with jaeger.connect() as query:
            trace = query.wait_for_trace(trace_id, timeout=60, until=lambda t: {"kong-gateway", "shipping-service"} <= t.services)
        expect(trace is not None, f"trace {trace_id} not served by Jaeger Query")
        state["trace_id"] = trace_id
        today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
        # Spans from different producers arrive a moment apart; wait until the stored trace is stable.
        time.sleep(5)
        with portforward.forward(kube.NS_OBSERVABILITY, "svc/opensearch-traces", 9200) as port, opensearch_client(port) as search:
            search.post(f"/{INDEX_PREFIX}-jaeger-span-*/_refresh")
            hits = search.post(f"/{INDEX_PREFIX}-jaeger-span-*/_count", json={"query": {"term": {"traceID": trace_id}}}).raise_for_status().json()["count"]
            indices = [row["index"] for row in search.get("/_cat/indices", params={"format": "json"}).json()]
        with jaeger.connect() as query:
            trace = query.get_trace(trace_id)
        expect(trace is not None and hits == len(trace.spans), f"OpenSearch holds {hits} spans, Query served {len(trace.spans) if trace else 0}")
        expect(f"{INDEX_PREFIX}-jaeger-span-{today}" in indices, f"today's span index missing from {indices}")
        return f"{hits} spans of trace {trace_id} in {INDEX_PREFIX}-jaeger-span-{today}"

    @suite.check("Prometheus scrapes Collector and Query telemetry")
    def scrape_targets() -> str:
        with prometheus.connect() as prom:
            targets = {t["labels"]["job"]: t["health"] for t in prom.targets()}
        for job in ("jaeger-collector", "jaeger-query"):
            expect(targets.get(job) == "up", f"target {job} is {targets.get(job)}")
        return ", ".join(f"{job}={health}" for job, health in sorted(targets.items()))

    @suite.check("Collector accepts spans without refusals or export failures")
    def collector_health() -> str:
        accepted = sent = 0.0
        with prometheus.connect() as prom:
            # The counters reach Prometheus with the next scrape (every 15 s) after the order above.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                accepted = prom.total(prometheus.metric_selector("otelcol_receiver_accepted_spans"))
                sent = prom.total(prometheus.metric_selector("otelcol_exporter_sent_spans"))
                if accepted > 0 and sent > 0:
                    break
                time.sleep(5)
            refused = prom.total(prometheus.metric_selector("otelcol_receiver_refused_spans"))
            failed = prom.total(prometheus.metric_selector("otelcol_exporter_send_failed_spans"))
        expect(accepted > 0 and sent > 0, f"accepted={accepted}, sent={sent}")
        expect(refused == 0 and failed == 0, f"refused={refused}, export failures={failed}")
        return f"accepted={accepted:.0f}, exported={sent:.0f}, refused=0, failed=0"

    @suite.check("retention job deletes trace indices of its own prefix only")
    def retention() -> str:
        # The cleaner picks indices by creation time, so an "old" index cannot be faked by its name.
        # Instead the real job template runs once with a throwaway prefix and zero retention days:
        # this proves network access, index matching and deletion without touching platform data.
        prefix = "retention-check"
        today = f"{dt.datetime.now(dt.UTC):%Y-%m-%d}"
        throwaway, platform_index = f"{prefix}-jaeger-span-{today}", f"{INDEX_PREFIX}-jaeger-span-{today}"
        api = kube.clients()
        cron = api.batch.read_namespaced_cron_job("jaeger-index-cleaner", kube.NS_OBSERVABILITY)
        job_spec = cron.spec.job_template.spec
        container = job_spec.template.spec.containers[0]
        retention_days = container.args[0]
        container.args = ["0", *container.args[1:]]
        container.env = [client.V1EnvVar(name="INDEX_PREFIX", value=prefix)]
        job_name = f"index-cleaner-check-{uuid.uuid4().hex[:6]}"
        with portforward.forward(kube.NS_OBSERVABILITY, "svc/opensearch-traces", 9200) as port, opensearch_client(port) as search:
            search.put(f"/{throwaway}", json={"settings": {"number_of_shards": 1, "number_of_replicas": 0}}).raise_for_status()
            api.batch.create_namespaced_job(kube.NS_OBSERVABILITY, client.V1Job(
                metadata=client.V1ObjectMeta(name=job_name, labels={"app.kubernetes.io/part-of": "txplatform"}),
                spec=job_spec,
            ))
            try:
                kube.wait_for(
                    lambda: (api.batch.read_namespaced_job(job_name, kube.NS_OBSERVABILITY).status.succeeded or 0) > 0,
                    timeout=180, description="manual index-cleaner run",
                )
            finally:
                api.batch.delete_namespaced_job(job_name, kube.NS_OBSERVABILITY, propagation_policy="Background")
            indices = {row["index"] for row in search.get("/_cat/indices", params={"format": "json"}).json()}
        expect(throwaway not in indices, f"{throwaway} survived the cleaner")
        expect(platform_index in indices, f"{platform_index} was deleted although it has another prefix")
        return f"deleted {throwaway}, kept {platform_index}; the nightly job keeps {retention_days} days"

    @suite.check("only Collector, Query and the cleaner reach OpenSearch; Query and Prometheus stay private")
    def policies() -> str:
        opensearch = probes.Target(f"opensearch-traces.{kube.NS_OBSERVABILITY}.svc.cluster.local", 9200)
        query = probes.Target(f"jaeger-query.{kube.NS_OBSERVABILITY}.svc.cluster.local", 16686)
        prom = probes.Target(f"prometheus-server.{kube.NS_OBSERVABILITY}.svc.cluster.local", 9090)
        collector_otlp = probes.Target(f"jaeger-collector.{kube.NS_OBSERVABILITY}.svc.cluster.local", 4317)
        expectations = [
            (kube.NS_OBSERVABILITY, "jaeger-collector", opensearch, True),
            (kube.NS_OBSERVABILITY, "prometheus", opensearch, False),
            (kube.NS_APP, "order-service", opensearch, False),
            (kube.NS_APP, "order-service", query, False),
            (kube.NS_APP, "order-service", prom, False),
            (kube.NS_APP, "order-service", collector_otlp, True),
            ("default", None, collector_otlp, False),
        ]
        mismatches = []
        for namespace, identity_name, target, allowed in expectations:
            reachable = probes.run_tcp_probe(namespace, identity_name, [target])[str(target)]
            if reachable != allowed:
                mismatches.append(f"{identity_name or 'anonymous'}@{namespace} -> {target} is {'reachable' if reachable else 'blocked'}")
        expect(not mismatches, "; ".join(mismatches))
        return f"{len(expectations)} connections behave as declared"

    return suite
