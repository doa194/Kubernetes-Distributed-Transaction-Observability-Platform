"""Observability resilience suite (disruptive): storage persistence and independent Jaeger roles.

Restarts OpenSearch and scales the Collector and Query to zero one at a time, always restoring them.
"""

from __future__ import annotations

from txplatform import identity, jaeger, kube, orders
from txplatform.validation.framework import Suite, expect


def _scale(name: str, replicas: int) -> None:
    kube.clients().apps.patch_namespaced_deployment_scale(name, kube.NS_OBSERVABILITY, {"spec": {"replicas": replicas}})


def _wait_scaled_down(name: str) -> None:
    kube.wait_for(
        lambda: not kube.clients().core.list_namespaced_pod(kube.NS_OBSERVABILITY, label_selector=f"app.kubernetes.io/name={name}").items,
        timeout=120, description=f"{name} pods gone",
    )


def _order_trace(tokens: identity.TokenProvider) -> str:
    response = orders.place(tokens, debug=True)
    expect(response.status_code == 201, f"order returned {response.status_code}")
    return orders.trace_id_of(response)


def build() -> Suite:
    suite = Suite("observability-resilience", "OpenSearch persistence, Collector and Query fail independently")
    tokens = identity.TokenProvider()

    def restore() -> None:
        for name in ("jaeger-collector", "jaeger-query"):
            _scale(name, 1)
        for name in ("jaeger-collector", "jaeger-query"):
            kube.wait_deployment_available(kube.NS_OBSERVABILITY, name, timeout=300)

    suite.cleanup = restore

    @suite.check("stored traces survive an OpenSearch pod restart")
    def persistence() -> str:
        trace_id = _order_trace(tokens)
        with jaeger.connect() as query:
            before = query.wait_for_trace(trace_id, timeout=60, until=lambda t: "shipping-service" in t.services)
        expect(before is not None, "trace was not stored before the restart")
        pod = kube.clients().core.list_namespaced_pod(kube.NS_OBSERVABILITY, label_selector="txplatform.io/network-identity=opensearch").items[0]
        kube.clients().core.delete_namespaced_pod(pod.metadata.name, kube.NS_OBSERVABILITY)
        kube.wait_for(lambda: kube.clients().core.read_namespaced_pod(pod.metadata.name, kube.NS_OBSERVABILITY).metadata.uid != pod.metadata.uid,
                      timeout=180, description="replacement OpenSearch pod")
        kube.wait_statefulset_ready(kube.NS_OBSERVABILITY, "opensearch-traces", timeout=600)
        with jaeger.connect() as query:
            after = query.wait_for_trace(trace_id, timeout=120)
        expect(after is not None and len(after.spans) == len(before.spans), "trace lost or incomplete after the restart")
        return f"trace {trace_id} ({len(after.spans)} spans) still served"

    @suite.check("Query serves stored traces while the Collector is down")
    def query_without_collector() -> str:
        trace_id = _order_trace(tokens)
        with jaeger.connect() as query:
            expect(query.wait_for_trace(trace_id, timeout=60, until=lambda t: "shipping-service" in t.services) is not None, "trace not stored")
        _scale("jaeger-collector", 0)
        _wait_scaled_down("jaeger-collector")
        with jaeger.connect() as query:
            served = query.get_trace(trace_id)
        # Orders must keep working even though no spans can be exported right now.
        response = orders.place(tokens)
        _scale("jaeger-collector", 1)
        kube.wait_deployment_available(kube.NS_OBSERVABILITY, "jaeger-collector", timeout=300)
        expect(served is not None, "Query could not serve a stored trace without the Collector")
        expect(response.status_code == 201, f"order failed with {response.status_code} while the Collector was down")
        return "stored trace served and orders completed while the Collector was down"

    @suite.check("the Collector keeps ingesting while Query is down")
    def ingest_without_query() -> str:
        _scale("jaeger-query", 0)
        _wait_scaled_down("jaeger-query")
        trace_id = _order_trace(tokens)
        _scale("jaeger-query", 1)
        kube.wait_deployment_available(kube.NS_OBSERVABILITY, "jaeger-query", timeout=300)
        with jaeger.connect() as query:
            trace = query.wait_for_trace(trace_id, timeout=90, until=lambda t: "shipping-service" in t.services)
        expect(trace is not None, "trace produced while Query was down was not stored")
        return f"trace {trace_id} ingested while Query was down"

    return suite
