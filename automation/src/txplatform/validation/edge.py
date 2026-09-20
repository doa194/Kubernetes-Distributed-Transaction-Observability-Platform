"""Edge suite: Kong as the only public entry point for the Order API.

Checks the Gateway API resources, TLS, routing, route attachment rules, forwarded metadata,
correlation ids, Kong's upstream retry setting, Kong as the root of every trace, and the gateway
network policies. Rate limiting and payload limits are exercised by the scenario catalog.
"""

from __future__ import annotations

import socket
import uuid

import httpx
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from txplatform import identity, jaeger, kube, portforward, probes
from txplatform.validation.framework import Suite, expect

GATEWAY_GROUP = "gateway.networking.k8s.io"
GATEWAY = ("gateway-system", "platform-gateway")
NORMAL_ORDER = {"items": [{"sku": "SKU-1001", "quantity": 1}], "paymentToken": "tok_test_approved", "shippingZone": "domestic"}


def _condition(conditions: list[dict] | None, kind: str) -> dict:
    return next((c for c in conditions or [] if c.get("type") == kind), {})


def build() -> Suite:
    suite = Suite("edge", "Gateway API routing, TLS, forwarded metadata, correlation, Kong trace root, gateway policies")
    tokens = identity.TokenProvider()
    probe_route = ("default", f"route-attachment-check-{uuid.uuid4().hex[:6]}")

    def cleanup() -> None:
        try:
            kube.clients().custom.delete_namespaced_custom_object(GATEWAY_GROUP, "v1", probe_route[0], "httproutes", probe_route[1])
        except ApiException:
            pass

    suite.cleanup = cleanup

    def gateway() -> httpx.Client:
        return identity.https_client(identity.GATEWAY_URL)

    @suite.check("Gateway and HTTPRoute are accepted and programmed")
    def gateway_status() -> str:
        custom = kube.clients().custom
        gw = custom.get_namespaced_custom_object(GATEWAY_GROUP, "v1", GATEWAY[0], "gateways", GATEWAY[1])
        expect(_condition(gw["status"].get("conditions"), "Programmed").get("status") == "True", f"Gateway not programmed: {gw['status'].get('conditions')}")
        route = custom.get_namespaced_custom_object(GATEWAY_GROUP, "v1", kube.NS_APP, "httproutes", "order-api")
        parent = next(p for p in route["status"]["parents"] if p["parentRef"]["name"] == GATEWAY[1])
        for kind in ("Accepted", "ResolvedRefs"):
            expect(_condition(parent["conditions"], kind).get("status") == "True", f"HTTPRoute {kind} is not True: {parent['conditions']}")
        return "Gateway Programmed; HTTPRoute Accepted and ResolvedRefs"

    @suite.check("HTTPS on 8443 uses TLS 1.2+ with the local CA certificate")
    def tls() -> str:
        with socket.create_connection(("127.0.0.1", 8443), timeout=10) as raw:
            with identity.ca_context().wrap_socket(raw, server_hostname="localhost") as tls_socket:
                version = tls_socket.version()
                names = dict(tls_socket.getpeercert()["subjectAltName"])
        expect(version in {"TLSv1.2", "TLSv1.3"}, f"negotiated {version}")
        expect(names.get("DNS") == "localhost", f"certificate names {names}")
        proxy = kube.clients().core.read_namespaced_service("kong-gateway-proxy", kube.NS_GATEWAY)
        ports = [(p.port, p.node_port) for p in proxy.spec.ports]
        expect(ports == [(443, 30443)], f"proxy service exposes {ports}; only HTTPS 443 -> 30443 is expected")
        return f"{version}, only the HTTPS listener is exposed"

    @suite.check("only /orders is routed, and it reaches OrderService's authentication")
    def routing() -> str:
        with gateway() as http:
            unauthenticated = http.post("/orders", json=NORMAL_ORDER)
            unknown = http.get("/does-not-exist")
            internal = http.get("/inventory/reservations")
        expect(unauthenticated.status_code == 401 and "Bearer" in unauthenticated.headers.get("www-authenticate", ""),
               f"/orders without token returned {unauthenticated.status_code}")
        expect(unknown.status_code == 404 and internal.status_code == 404, f"unrouted paths returned {unknown.status_code}/{internal.status_code}")
        return "401 from OrderService for /orders, 404 from Kong otherwise"

    @suite.check("routes from namespaces without gateway access are rejected")
    def route_attachment() -> str:
        custom = kube.clients().custom
        custom.create_namespaced_custom_object(GATEWAY_GROUP, "v1", probe_route[0], "httproutes", {
            "apiVersion": f"{GATEWAY_GROUP}/v1", "kind": "HTTPRoute",
            "metadata": {"name": probe_route[1]},
            "spec": {
                "parentRefs": [{"name": GATEWAY[1], "namespace": GATEWAY[0], "sectionName": "https"}],
                "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/hijack"}}], "backendRefs": [{"name": "kubernetes", "port": 443}]}],
            },
        })

        def rejected():
            status = custom.get_namespaced_custom_object(GATEWAY_GROUP, "v1", probe_route[0], "httproutes", probe_route[1]).get("status", {})
            for parent in status.get("parents", []):
                accepted = _condition(parent.get("conditions"), "Accepted")
                if accepted.get("status") == "False":
                    return accepted.get("reason")
            return None

        reason = kube.wait_for(rejected, timeout=60, description="route attachment decision")
        # Kong's controller reports a listener that rejects the namespace as NoMatchingParent; the
        # Gateway API also allows NotAllowedByListeners. The accepted order-api route with the same
        # parent reference (first check) shows the rejection comes from the namespace restriction.
        expect(reason in {"NotAllowedByListeners", "NoMatchingParent"}, f"unexpected rejection reason {reason}")
        with gateway() as http:
            expect(http.get("/hijack").status_code == 404, "the rejected route is being served")
        return f"rejected with {reason}"

    @suite.check("correlation ids are generated when absent and preserved when valid")
    def correlation() -> str:
        with gateway() as http:
            generated = http.post("/orders", json=NORMAL_ORDER).headers.get("x-correlation-id", "")
            preserved = http.post("/orders", json=NORMAL_ORDER, headers={"X-Correlation-ID": "edge-check-123"}).headers.get("x-correlation-id")
        expect(len(generated) >= 32, f"generated correlation id '{generated}'")
        expect(preserved == "edge-check-123", f"client correlation id came back as '{preserved}'")
        return f"generated {generated}"

    @suite.check("Location uses the public HTTPS endpoint; spoofed forwarding headers are ignored")
    def forwarded_metadata() -> str:
        headers = {"Authorization": f"Bearer {tokens.token('scenario-runner')}"}
        with gateway() as http:
            through_kong = http.post("/orders", json=NORMAL_ORDER, headers=headers)
        expect(through_kong.status_code == 201, f"order through Kong returned {through_kong.status_code}")
        expect(through_kong.headers["location"].startswith("https://localhost:8443/orders/"), f"Location is {through_kong.headers['location']}")
        # A direct connection is not from the trusted proxy network, so its X-Forwarded-Proto must be ignored.
        with portforward.forward(kube.NS_APP, "svc/order-service", 8080) as port:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as direct:
                spoofed = direct.post("/orders", json=NORMAL_ORDER, headers={**headers, "X-Forwarded-Proto": "https"})
        expect(spoofed.headers["location"].startswith("http://"), f"spoofed request produced Location {spoofed.headers['location']}")
        return through_kong.headers["location"]

    @suite.check("Kong never retries order requests upstream")
    def kong_retries() -> str:
        with portforward.forward(kube.NS_GATEWAY, "svc/kong-gateway-admin", 8444) as port:
            # Operator-only access to Kong's internal Admin API, which uses a self-signed certificate.
            with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=False, timeout=30) as admin:
                services = admin.get("/services").raise_for_status().json()["data"]
        # The controller names Kong services after the HTTPRoute rule that references the backend.
        order_services = [s for s in services if s.get("name", "").startswith(f"httproute.{kube.NS_APP}.order-api.")]
        expect(order_services, f"no Kong service for the order-api route among {[s.get('name') for s in services]}")
        retries = {s["name"]: s["retries"] for s in order_services}
        expect(all(value == 0 for value in retries.values()), f"retries: {retries}")
        return f"retries {retries}"

    @suite.check("Kong starts every trace and ignores client trace context")
    def kong_trace_root() -> str:
        client_trace = "0af7651916cd43dd8448eb211c80319c"
        with gateway() as http:
            response = http.post("/orders", json=NORMAL_ORDER, headers={
                "Authorization": f"Bearer {tokens.token('scenario-runner')}",
                "traceparent": f"00-{client_trace}-b7ad6b7169203331-01",
                "baggage": "scenario.id=spoofed-by-client",
                "X-Debug-Trace": "true",
            })
        expect(response.status_code == 201, f"order returned {response.status_code}")
        trace_id = response.headers["traceresponse"].split("-")[1]
        kong_request_id = response.headers.get("x-kong-request-id", "")
        expect(trace_id != client_trace, "Kong continued the client's trace id")
        with jaeger.connect() as query:
            trace = query.wait_for_trace(trace_id, timeout=60, until=lambda t: "kong-gateway" in t.services and "shipping-service" in t.services)
        expect(trace is not None, f"trace {trace_id} not found")
        roots = trace.roots()
        expect(len(roots) == 1 and roots[0].service == "kong-gateway", f"roots: {[(r.service, r.name) for r in roots]}")
        expect(roots[0].attributes.get("kong.request.id") == kong_request_id, f"kong.request.id {roots[0].attributes.get('kong.request.id')} != header {kong_request_id}")
        order_server = trace.by_service("order-service", kind="SERVER")[0]
        ancestor = trace.parent_of(order_server)
        while ancestor is not None and ancestor.service != "kong-gateway":
            ancestor = trace.parent_of(ancestor)
        expect(ancestor is not None, "OrderService server span does not descend from a Kong span")
        expect(order_server.attributes.get("kong.request.id") == kong_request_id, "OrderService did not record the Kong request id")
        expect(all(s.attributes.get("scenario.id") != "spoofed-by-client" for s in trace.spans), "client baggage reached a span")
        return f"root {roots[0].name} (kong.request.id={kong_request_id})"

    @suite.check("gateway network policies limit Kong to its intended connections")
    def gateway_policies() -> str:
        kong_targets = [
            probes.Target(f"order-service.{kube.NS_APP}.svc.cluster.local", 8080),
            probes.Target(f"inventory-service.{kube.NS_APP}.svc.cluster.local", 8080),
            probes.Target(f"keycloak.{kube.NS_APP}.svc.cluster.local", 8080),
        ]
        results = probes.run_tcp_probe(kube.NS_GATEWAY, "kong-proxy", kong_targets)
        expected = [True, False, False]
        mismatches = [f"{t} {'reachable' if results[str(t)] else 'blocked'}" for t, e in zip(kong_targets, expected) if results[str(t)] != e]
        admin_pod = kube.ready_pods(kube.NS_GATEWAY, "txplatform.io/network-identity=kong-proxy")[0]
        admin = probes.Target(admin_pod.status.pod_ip, 8444)
        if probes.run_tcp_probe(kube.NS_GATEWAY, None, [admin])[str(admin)]:
            mismatches.append("Kong Admin API reachable from a non-controller pod")
        expect(not mismatches, "; ".join(mismatches))
        return "proxy -> order-service only; Admin API only for the controller"

    return suite
