"""Tracing suite: a real order produces a complete, Kubernetes-aware trace in Jaeger, and
health probes produce no trace data at all."""

from __future__ import annotations

import uuid

import httpx

from txplatform import identity, jaeger, kube, portforward
from txplatform.validation.framework import Suite, expect

DOTNET_SERVICES = {"order-service", "inventory-service", "fraud-service", "payment-service", "shipping-service"}
K8S_RESOURCE_KEYS = ("k8s.namespace.name", "k8s.pod.name", "k8s.node.name", "k8s.deployment.name")
NORMAL_ORDER = {"items": [{"sku": "SKU-1001", "quantity": 1}], "paymentToken": "tok_test_approved", "shippingZone": "domestic"}


def trace_id_from_traceresponse(header: str | None) -> str:
    parts = (header or "").split("-")
    return parts[1] if len(parts) == 4 else ""


def build() -> Suite:
    suite = Suite("tracing", "complete Kubernetes-aware traces in Jaeger, no traces from health probes")
    state: dict[str, object] = {}

    def order_trace():
        if "trace" in state:
            return state["trace"]
        token = identity.TokenProvider().token("scenario-runner")
        run_id = f"validate-{uuid.uuid4().hex[:8]}"
        with portforward.forward(kube.NS_APP, "svc/order-service", 8080) as port:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
                # The debug flag makes tail sampling keep this trace; ordinary traces are only sampled.
                response = client.post("/orders", json=NORMAL_ORDER, headers={
                    "Authorization": f"Bearer {token}", "X-Scenario-Id": "tracing-validation", "X-Scenario-Run-Id": run_id,
                    "X-Debug-Trace": "true",
                })
        expect(response.status_code == 201, f"order returned {response.status_code}")
        trace_id = trace_id_from_traceresponse(response.headers.get("traceresponse"))
        expect(bool(trace_id), "OrderService did not return a traceresponse header")
        with jaeger.connect() as client:
            trace = client.wait_for_trace(trace_id, timeout=60, until=lambda t: DOTNET_SERVICES <= t.services)
        expect(trace is not None, f"trace {trace_id} not found in Jaeger within 60s")
        state["trace"] = trace
        return trace

    @suite.check("order trace reaches Jaeger with all five services")
    def complete_trace() -> str:
        trace = order_trace()
        missing = DOTNET_SERVICES - trace.services
        expect(not missing, f"trace {trace.trace_id} is missing {sorted(missing)}")
        return f"trace {trace.trace_id}: {len(trace.spans)} spans"

    @suite.check("spans carry Kubernetes metadata matching the live pods")
    def kubernetes_metadata() -> str:
        trace = order_trace()
        pods = {p.metadata.name: p.spec.node_name for p in kube.clients().core.list_namespaced_pod(kube.NS_APP).items}
        for span in trace.spans:
            if span.service not in DOTNET_SERVICES:
                continue
            missing = [key for key in K8S_RESOURCE_KEYS if not span.resource.get(key)]
            expect(not missing, f"{span.service} span '{span.name}' lacks {missing}")
            pod = span.resource["k8s.pod.name"]
            expect(pod in pods, f"span names pod {pod}, which does not exist")
            expect(pods[pod] == span.resource["k8s.node.name"], f"pod {pod} runs on {pods[pod]}, span says {span.resource['k8s.node.name']}")
        return "namespace, pod, node and deployment present and correct"

    @suite.check("health probes produce no operations in Jaeger")
    def no_probe_traces() -> str:
        with jaeger.connect() as client:
            services = set(client.services()) & DOTNET_SERVICES
            probe_operations = [f"{s}:{op}" for s in services for op in client.operations(s) if "/health" in op or "/internal" in op]
        expect(not probe_operations, f"probe operations found: {probe_operations}")
        return f"{len(services)} services checked"

    return suite
