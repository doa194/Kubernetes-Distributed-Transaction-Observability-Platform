"""Security suite: identity, API authorization and network boundaries of the running platform.

Checks use the real endpoints: tokens come from Keycloak over verified TLS, authorization is
tested against the deployed OrderService, and network rules are tested with probe pods that
impersonate specific workloads.
"""

from __future__ import annotations

import json
import uuid

import httpx

from txplatform import helm, identity, kube, portforward, probes, secrets
from txplatform.validation.framework import Suite, expect

SERVICES = ("order-service", "inventory-service", "fraud-service", "payment-service", "shipping-service")
NORMAL_ORDER = {"items": [{"sku": "SKU-1001", "quantity": 1}], "paymentToken": "tok_test_approved", "shippingZone": "domestic"}


def _svc(name: str, port: int, namespace: str = kube.NS_APP) -> probes.Target:
    return probes.Target(f"{name}.{namespace}.svc.cluster.local", port)


def _pod_ip(name: str) -> str:
    pods = kube.ready_pods(kube.NS_APP, f"app.kubernetes.io/name={name}")
    expect(bool(pods), f"no ready pod for {name}")
    return pods[0].status.pod_ip


def expected_network_matrix() -> list[tuple[str, str | None, probes.Target, bool]]:
    """(namespace, identity, target, should_connect) rules the NetworkPolicies must enforce."""
    inventory_ip = _pod_ip("inventory-service")
    rules: list[tuple[str, str | None, probes.Target, bool]] = []
    # OrderService may call its dependencies and Keycloak's internal port...
    for name in SERVICES[1:]:
        rules.append((kube.NS_APP, "order-service", _svc(name, 8080), True))
    rules.append((kube.NS_APP, "order-service", _svc("keycloak", 8080), True))
    # ...but no pod may reach a management port, not even OrderService.
    rules.append((kube.NS_APP, "order-service", probes.Target(inventory_ip, 8081), False))
    # Private services cannot call each other, OrderService or Keycloak's internal port.
    rules += [
        (kube.NS_APP, "inventory-service", _svc("payment-service", 8080), False),
        (kube.NS_APP, "inventory-service", _svc("order-service", 8080), False),
        (kube.NS_APP, "inventory-service", _svc("keycloak", 8080), False),
    ]
    # A pod without an identity, from another namespace, reaches none of the private ports.
    for target in (_svc("order-service", 8080), _svc("inventory-service", 8080), _svc("keycloak", 8080)):
        rules.append(("default", None, target, False))
    # Keycloak's HTTPS port is the intended public entry point.
    rules.append(("default", None, _svc("keycloak-public", 8443), True))
    return rules


def build() -> Suite:
    suite = Suite("security", "identity provider, API authorization, network policy matrix, secret handling")
    tokens = identity.TokenProvider()

    @suite.check("Keycloak serves verified TLS with the public issuer")
    def public_discovery() -> str:
        with identity.https_client(identity.KEYCLOAK_PUBLIC_URL) as client:
            document = client.get(f"/realms/{identity.REALM}/.well-known/openid-configuration").raise_for_status().json()
        expect(document["issuer"] == identity.ISSUER, f"issuer is {document['issuer']}")
        return document["issuer"]

    @suite.check("in-cluster discovery uses public issuer and internal key endpoint")
    def internal_discovery() -> str:
        url = f"http://keycloak.{kube.NS_APP}.svc.cluster.local:8080/realms/{identity.REALM}/.well-known/openid-configuration"
        [(ok, body)] = probes.run_http_probe(kube.NS_APP, "order-service", [url])
        expect(ok, f"OrderService identity could not fetch discovery document: {body[:200]}")
        document = json.loads(body)
        expect(document["issuer"] == identity.ISSUER, f"issuer is {document['issuer']}")
        expect(document["jwks_uri"].startswith("http://keycloak."), f"jwks_uri is {document['jwks_uri']}")
        return f"jwks_uri={document['jwks_uri']}"

    @suite.check("client tokens carry order-api audience and exact permissions")
    def token_claims() -> str:
        expected = {"scenario-runner": {"orders.read", "orders.write", "traces.debug"}, "order-reader": {"orders.read"}}
        for client_id, permissions in expected.items():
            claims = identity.decode_claims(tokens.token(client_id))
            audience = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
            expect("order-api" in audience, f"{client_id} token audience is {audience}")
            expect(set(claims.get("permissions", [])) == permissions, f"{client_id} permissions are {claims.get('permissions')}")
            expect(claims["iss"] == identity.ISSUER, f"{client_id} issuer is {claims['iss']}")
        return "scenario-runner and order-reader tokens as expected"

    @suite.check("OrderService enforces authentication and permissions")
    def api_authorization() -> str:
        with portforward.forward(kube.NS_APP, "svc/order-service", 8080) as port:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
                anonymous = client.post("/orders", json=NORMAL_ORDER).status_code
                reader = {"Authorization": f"Bearer {tokens.token('order-reader')}"}
                reader_create = client.post("/orders", json=NORMAL_ORDER, headers=reader).status_code
                reader_read = client.get(f"/orders/{uuid.uuid4()}", headers=reader).status_code
                runner = {"Authorization": f"Bearer {tokens.token('scenario-runner')}"}
                runner_create = client.post("/orders", json=NORMAL_ORDER, headers=runner).status_code
        outcome = (anonymous, reader_create, reader_read, runner_create)
        expect(outcome == (401, 403, 404, 201), f"(anonymous, reader POST, reader GET, runner POST) = {outcome}")
        return "401 / 403 / 404 / 201"

    @suite.check("network policies enforce the connection matrix")
    def network_matrix() -> str:
        rules = expected_network_matrix()
        groups: dict[tuple[str, str | None], list[tuple[probes.Target, bool]]] = {}
        for namespace, identity_name, target, allowed in rules:
            groups.setdefault((namespace, identity_name), []).append((target, allowed))
        mismatches = []
        for (namespace, identity_name), expectations in groups.items():
            results = probes.run_tcp_probe(namespace, identity_name, [t for t, _ in expectations])
            for target, allowed in expectations:
                if results[str(target)] != allowed:
                    state = "reachable" if results[str(target)] else "blocked"
                    mismatches.append(f"{identity_name or 'anonymous'}@{namespace} -> {target} is {state}")
        expect(not mismatches, "; ".join(mismatches))
        return f"{len(rules)} connections behave as declared"

    @suite.check("only the gateway and the identity provider are reachable from outside the cluster")
    def exposure() -> str:
        # Anything reachable from the host must be a NodePort or LoadBalancer Service; kind maps only
        # the two node ports below to loopback (checked by the foundation suite).
        exposed = sorted(
            (service.metadata.namespace, service.metadata.name, port.node_port)
            for service in kube.clients().core.list_service_for_all_namespaces().items
            if service.spec.type in {"NodePort", "LoadBalancer"}
            for port in service.spec.ports
        )
        expected = [(kube.NS_GATEWAY, "kong-gateway-proxy", 30443), (kube.NS_APP, "keycloak-public", 31443)]
        expect(exposed == expected, f"externally exposed service ports {exposed}, expected {expected}")
        return "; ".join(f"{namespace}/{name}:{port}" for namespace, name, port in exposed)

    @suite.check("secret values never appear in ConfigMaps or Helm values")
    def secrets_not_leaked() -> str:
        values = [secrets.read_value(kube.NS_APP, identity.ADMIN_SECRET_NAME, "password")]
        values += [secrets.read_value(kube.NS_APP, identity.CLIENT_SECRETS_NAME, name) for name in identity.CLIENTS]
        leaks = []
        for namespace in kube.PLATFORM_NAMESPACES:
            for config_map in kube.clients().core.list_namespaced_config_map(namespace).items:
                content = json.dumps(config_map.data or {})
                if any(value in content for value in values):
                    leaks.append(f"ConfigMap {namespace}/{config_map.metadata.name}")
        for release in helm.list_releases():
            rendered = helm.get_values(release["name"], release["namespace"])
            if any(value in rendered for value in values):
                leaks.append(f"Helm release {release['name']}")
        expect(not leaks, f"secret values found in: {leaks}")
        return f"{len(values)} secret values checked"

    return suite
