"""Platform lifecycle: bootstrap, component deployment and destroy.

`bootstrap` performs the whole local workflow in a fixed order. Each step is idempotent,
so running bootstrap again only changes what actually differs. `deploy` runs selected
steps against an existing cluster.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass

from txplatform import console, helm, identity, images, kind, kube, paths, pki, preflight, secrets, tools, versions


@dataclass(frozen=True)
class Component:
    name: str
    description: str
    deploy: Callable[[], None]


def deploy_foundation() -> None:
    helm.upgrade_install(
        "platform-foundation",
        str(paths.charts_dir() / "platform-foundation"),
        "default",
    )


def deploy_certificates() -> None:
    result = pki.ensure()
    console.info(f"local CA: {result.ca_action}, server certificate: {result.leaf_action}")
    cert = result.files.tls_cert.read_bytes()
    key = result.files.tls_key.read_bytes()
    for namespace, name in ((kube.NS_GATEWAY, "edge-tls"), (kube.NS_APP, "keycloak-tls")):
        outcome = secrets.apply_tls_secret(namespace, name, cert, key)
        console.info(f"secret {namespace}/{name}: {outcome}")


def deploy_crds() -> None:
    # CRDs come first so the gateway chart (Gateway, KongClusterPlugin, network policies) can be installed
    # before Kong starts; Kong's controller needs those network policies to reach Kong's Admin API.
    # Server-side apply keeps the large CRD manifests idempotent.
    pins = versions.load()
    kube.kubectl("apply", "--server-side", "--force-conflicts", "-f", str(paths.repo_root() / pins.gatewayApi.standardCrds), timeout=300)
    helm.ensure_repo("kong", pins.charts.kong.repository)
    kong_crds = tools.run("helm", "show", "crds", f"kong/{pins.charts.kong.chart}", "--version", pins.charts.kong.version, timeout=300).stdout
    kube.kubectl("apply", "--server-side", "--force-conflicts", "-f", "-", input_text=kong_crds, timeout=300)


def deploy_kong() -> None:
    pins = versions.load()
    chart = pins.charts.kong
    helm.ensure_repo("kong", chart.repository)
    gateway_repo, gateway_tag = versions.split_image(pins.images.kongGateway)
    controller_repo, controller_tag = versions.split_image(pins.images.kongIngressController)
    helm.upgrade_install(
        "kong",
        f"kong/{chart.chart}",
        kube.NS_GATEWAY,
        version=chart.version,
        values_files=[paths.local_values_dir() / "kong.yaml"],
        set_values={
            "gateway.image.repository": gateway_repo,
            "gateway.image.tag": gateway_tag,
            "controller.ingressController.image.repository": controller_repo,
            "controller.ingressController.image.tag": controller_tag,
        },
    )
    for deployment in ("kong-controller", "kong-gateway"):
        kube.wait_deployment_available(kube.NS_GATEWAY, deployment, timeout=300)


def deploy_gateway() -> None:
    helm.upgrade_install("gateway", str(paths.charts_dir() / "gateway"), kube.NS_GATEWAY)


def deploy_identity() -> None:
    admin = secrets.ensure_generated_secret(kube.NS_APP, identity.ADMIN_SECRET_NAME, {
        "username": lambda: "platform-admin",
        "password": secrets.generate_secret_value,
    })
    clients = secrets.ensure_generated_secret(kube.NS_APP, identity.CLIENT_SECRETS_NAME, {
        name: secrets.generate_secret_value for name in identity.CLIENTS
    })
    console.info(f"secret {identity.ADMIN_SECRET_NAME}: {admin}, secret {identity.CLIENT_SECRETS_NAME}: {clients}")
    helm.upgrade_install(
        "identity",
        str(paths.charts_dir() / "identity"),
        kube.NS_APP,
        set_values={"image": versions.load().images.keycloak},
        timeout="15m",
    )
    kube.wait_deployment_available(kube.NS_APP, "keycloak", timeout=600)


def deploy_observability() -> None:
    pins = versions.load()
    helm.ensure_repo("opensearch", pins.charts.opensearch.repository)
    helm.ensure_repo("prometheus-community", pins.charts.prometheus.repository)

    opensearch_repo, opensearch_tag = versions.split_image(pins.images.opensearch)
    helm.upgrade_install(
        "opensearch",
        f"opensearch/{pins.charts.opensearch.chart}",
        kube.NS_OBSERVABILITY,
        version=pins.charts.opensearch.version,
        values_files=[paths.local_values_dir() / "opensearch.yaml"],
        set_values={"image.repository": opensearch_repo, "image.tag": opensearch_tag},
        timeout="15m",
    )
    kube.wait_statefulset_ready(kube.NS_OBSERVABILITY, "opensearch-traces", timeout=900)

    helm.upgrade_install(
        "observability",
        str(paths.charts_dir() / "observability"),
        kube.NS_OBSERVABILITY,
        set_values={"image": pins.images.jaeger, "indexCleanerImage": pins.images.jaegerIndexCleaner},
    )
    for deployment in ("jaeger-collector", "jaeger-query"):
        kube.wait_deployment_available(kube.NS_OBSERVABILITY, deployment, timeout=300)

    prometheus_repo, prometheus_tag = versions.split_image(pins.images.prometheus)
    helm.upgrade_install(
        "prometheus",
        f"prometheus-community/{pins.charts.prometheus.chart}",
        kube.NS_OBSERVABILITY,
        version=pins.charts.prometheus.version,
        values_files=[paths.local_values_dir() / "prometheus.yaml"],
        set_values={"server.image.repository": prometheus_repo, "server.image.tag": prometheus_tag},
    )
    kube.wait_deployment_available(kube.NS_OBSERVABILITY, "prometheus-server", timeout=300)


def deploy_services() -> None:
    tags = images.ensure_service_images()
    for service, tag in tags.items():
        kind.load_image(images.image_name(service, tag))
    helm.upgrade_install(
        "transaction-services",
        str(paths.charts_dir() / "transaction-services"),
        kube.NS_APP,
        values_files=[paths.local_values_dir() / "transaction-services.yaml"],
        set_values={f"services.{service}.tag": tag for service, tag in tags.items()},
    )
    for service in images.SERVICES:
        kube.wait_deployment_available(kube.NS_APP, service, timeout=300)


COMPONENTS: list[Component] = [
    Component("foundation", "namespaces, Pod Security levels, default limits", deploy_foundation),
    Component("certificates", "local CA, server certificate and TLS secrets", deploy_certificates),
    Component("crds", "Gateway API and Kong custom resource definitions", deploy_crds),
    Component("gateway", "GatewayClass, HTTPS Gateway, global tracing plugin, gateway network policies", deploy_gateway),
    Component("kong", "Kong Ingress Controller and DB-less Kong Gateway", deploy_kong),
    Component("identity", "Keycloak with the transaction-platform realm", deploy_identity),
    Component("observability", "OpenSearch, Jaeger Collector and Query, Prometheus", deploy_observability),
    Component("services", "build, load and deploy the five .NET services", deploy_services),
]


def component_names() -> list[str]:
    return [c.name for c in COMPONENTS]


def ensure_cluster() -> None:
    if kind.cluster_exists():
        console.info(f"kind cluster '{kube.CLUSTER_NAME}' already exists")
    else:
        console.info(f"creating kind cluster '{kube.CLUSTER_NAME}' (this pulls the node image on first run)")
        kind.create_cluster()
    kube.guard()
    # kind only waits for the control plane; workloads need the workers too.
    kube.wait_nodes_ready()


def deploy(names: list[str]) -> None:
    kube.guard()
    selected = [c for c in COMPONENTS if c.name in names]
    unknown = set(names) - {c.name for c in COMPONENTS}
    if unknown:
        raise ValueError(f"unknown components: {sorted(unknown)}; choose from {component_names()}")
    for component in selected:
        console.step(f"Deploying {component.name}: {component.description}")
        component.deploy()
        console.ok(component.name)


def bootstrap() -> bool:
    console.step("Preflight")
    findings = preflight.run()
    failures = [f for f in findings if f.level is preflight.Level.FAIL]
    for finding in failures:
        console.fail(f"{finding.check}: {finding.message}")
    if failures:
        return False
    console.step("Cluster")
    ensure_cluster()
    deploy(component_names())
    return True


def destroy(purge: bool) -> None:
    if kind.cluster_exists():
        console.step(f"Deleting kind cluster '{kube.CLUSTER_NAME}'")
        kind.delete_cluster()
    else:
        console.info("no project cluster exists")
    if purge:
        for folder in (paths.local_state_dir(),):
            if folder.exists():
                shutil.rmtree(folder)
                console.info(f"removed {folder}")
