"""Access to the project's Kubernetes cluster.

Every call is pinned to the project's kind context instead of kubectl's "current context".
Switching contexts in another terminal can therefore never redirect a change to another
cluster. The context guard additionally refuses to act unless that context points at a
local (loopback) API server, which is what kind creates.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import TypeVar
from urllib.parse import urlparse

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from txplatform import tools

CLUSTER_NAME = "txplatform"
CONTEXT = f"kind-{CLUSTER_NAME}"

NS_GATEWAY = "gateway-system"
NS_APP = "transaction-platform"
NS_OBSERVABILITY = "observability"
PLATFORM_NAMESPACES = (NS_GATEWAY, NS_APP, NS_OBSERVABILITY)

T = TypeVar("T")


class ContextGuardError(RuntimeError):
    """Raised when the Kubernetes context is not the local project cluster."""


def check_context_is_local(context_name: str, server_url: str) -> None:
    """Pure guard used before any change: only the loopback kind cluster is allowed."""
    if context_name != CONTEXT:
        raise ContextGuardError(f"refusing to use context '{context_name}'; only '{CONTEXT}' is allowed")
    host = urlparse(server_url).hostname or ""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ContextGuardError(
            f"context '{context_name}' points at '{server_url}', which is not a local kind cluster"
        )


def _context_server() -> str:
    # kubectl resolves merged kubeconfig files (KUBECONFIG) exactly like every other tool does.
    result = tools.run(
        "kubectl", "config", "view", "--minify", "--context", CONTEXT,
        "-o", "jsonpath={.clusters[0].cluster.server}", check=False, timeout=30,
    )
    server = result.stdout.strip()
    if result.returncode != 0 or not server:
        raise ContextGuardError(f"kube context '{CONTEXT}' does not exist; run `platformctl bootstrap` first")
    return server


def guard() -> None:
    check_context_is_local(CONTEXT, _context_server())


@dataclass(frozen=True)
class Clients:
    api: client.ApiClient
    core: client.CoreV1Api
    apps: client.AppsV1Api
    batch: client.BatchV1Api
    networking: client.NetworkingV1Api
    coordination: client.CoordinationV1Api
    custom: client.CustomObjectsApi
    storage: client.StorageV1Api


@cache
def clients() -> Clients:
    guard()
    api = config.new_client_from_config(context=CONTEXT)
    return Clients(
        api=api,
        core=client.CoreV1Api(api),
        apps=client.AppsV1Api(api),
        batch=client.BatchV1Api(api),
        networking=client.NetworkingV1Api(api),
        coordination=client.CoordinationV1Api(api),
        custom=client.CustomObjectsApi(api),
        storage=client.StorageV1Api(api),
    )


def reset_clients() -> None:
    """Forgets cached clients, e.g. after the cluster was recreated."""
    clients.cache_clear()


def kubectl(*args: str, check: bool = True, timeout: float | None = 300, input_text: str | None = None) -> tools.CommandResult:
    return tools.run("kubectl", "--context", CONTEXT, *args, check=check, timeout=timeout, input_text=input_text)


def wait_for(predicate: Callable[[], T | None], *, timeout: float, interval: float = 2.0, description: str) -> T:
    """Polls until the predicate returns a truthy value, or raises TimeoutError."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except ApiException as error:  # transient API errors while resources settle
            last_error = error
        time.sleep(interval)
    suffix = f" (last error: {last_error.reason})" if last_error else ""
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {description}{suffix}")


def deployment_available(namespace: str, name: str) -> bool:
    deployment = clients().apps.read_namespaced_deployment(name, namespace)
    desired = deployment.spec.replicas or 0
    status = deployment.status
    return (
        status.observed_generation is not None
        and status.observed_generation >= deployment.metadata.generation
        and (status.updated_replicas or 0) == desired
        and (status.available_replicas or 0) == desired
        and (status.replicas or 0) == desired
    )


def wait_deployment_available(namespace: str, name: str, timeout: float = 300) -> None:
    wait_for(lambda: deployment_available(namespace, name), timeout=timeout, description=f"deployment {namespace}/{name}")


def statefulset_ready(namespace: str, name: str) -> bool:
    statefulset = clients().apps.read_namespaced_stateful_set(name, namespace)
    desired = statefulset.spec.replicas or 0
    return (statefulset.status.ready_replicas or 0) == desired and (statefulset.status.current_replicas or 0) == desired


def wait_statefulset_ready(namespace: str, name: str, timeout: float = 600) -> None:
    wait_for(lambda: statefulset_ready(namespace, name), timeout=timeout, description=f"statefulset {namespace}/{name}")


def ready_pods(namespace: str, label_selector: str) -> list[client.V1Pod]:
    return [p for p in running_pods(namespace, label_selector) if is_pod_ready(p)]


def running_pods(namespace: str, label_selector: str) -> list[client.V1Pod]:
    """Pods whose containers run and that are not being deleted, whether or not they are ready."""
    pods = clients().core.list_namespaced_pod(namespace, label_selector=label_selector).items
    return [p for p in pods if p.metadata.deletion_timestamp is None and p.status.phase == "Running"]


def is_pod_ready(pod: client.V1Pod) -> bool:
    conditions = pod.status.conditions or []
    return any(c.type == "Ready" and c.status == "True" for c in conditions)


def pod_logs(namespace: str, name: str, container: str | None = None) -> str:
    # Reading the raw response avoids the client's lossy string conversion of log bodies.
    kwargs = {"container": container} if container else {}
    response = clients().core.read_namespaced_pod_log(name, namespace, _preload_content=False, **kwargs)
    return response.data.decode("utf-8", errors="replace")


def nodes_ready() -> bool:
    nodes = clients().core.list_node().items
    return bool(nodes) and all(
        any(c.type == "Ready" and c.status == "True" for c in (node.status.conditions or [])) for node in nodes
    )


def wait_nodes_ready(timeout: float = 300) -> None:
    wait_for(nodes_ready, timeout=timeout, description="all nodes Ready")


def namespace_exists(name: str) -> bool:
    try:
        clients().core.read_namespace(name)
        return True
    except ApiException as error:
        if error.status == 404:
            return False
        raise
