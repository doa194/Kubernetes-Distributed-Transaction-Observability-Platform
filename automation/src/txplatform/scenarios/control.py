"""Operator actions against the running platform: fault rules, circuit breakers, payment ledger
and Kubernetes workload changes.

Management ports are reached per pod through port-forwarding, never through a Service, so a fault
can target exactly one replica. Every change is journaled before it is made.
"""

from __future__ import annotations

import datetime as dt

import httpx
from kubernetes.client.exceptions import ApiException

from txplatform import kube, portforward
from txplatform.scenarios.journal import Journal
from txplatform.scenarios.schema import DOWNSTREAM_SERVICES, WORKLOADS, FaultSpec

MANAGEMENT_PORT = 8081


def _read_workload(name: str):
    namespace, kind = WORKLOADS[name]
    apps = kube.clients().apps
    return apps.read_namespaced_deployment(name, namespace) if kind == "Deployment" else apps.read_namespaced_stateful_set(name, namespace)


def _selector(name: str) -> str:
    labels = _read_workload(name).spec.selector.match_labels
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def ready_pods(workload: str) -> list[str]:
    return sorted(pod.metadata.name for pod in kube.ready_pods(WORKLOADS[workload][0], _selector(workload)))


def running_pods(workload: str) -> list[str]:
    # Faults are listed and removed on unready pods too: a readiness fault makes its own pod unready.
    return sorted(pod.metadata.name for pod in kube.running_pods(WORKLOADS[workload][0], _selector(workload)))


def _management(pod: str, method: str, path: str, body: dict | None = None) -> httpx.Response:
    with portforward.forward(kube.NS_APP, f"pod/{pod}", MANAGEMENT_PORT) as port:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            return client.request(method, path, json=body)


def apply_fault(spec: FaultSpec, run_id: str, journal: Journal) -> list[str]:
    pods = ready_pods(spec.service)
    if not pods:
        raise RuntimeError(f"no ready pod of {spec.service} to inject a fault into")
    targets = pods if spec.replicas == "all" else pods[:1]
    for pod in targets:
        journal.record("fault", service=spec.service, pod=pod, operation=spec.operation, mode=spec.mode)
        response = _management(pod, "PUT", "/internal/faults", spec.to_rule(run_id))
        if response.status_code != 201:
            raise RuntimeError(f"fault rule rejected by {pod}: {response.status_code} {response.text[:300]}")
    return targets


def list_faults(service: str) -> dict[str, list[dict]]:
    return {pod: _management(pod, "GET", "/internal/faults").raise_for_status().json() for pod in running_pods(service)}


def clear_faults(service: str) -> int:
    removed = 0
    for pod in running_pods(service):
        removed += int(_management(pod, "DELETE", "/internal/faults").raise_for_status().json().get("removed", 0))
    return removed


def leftover_faults() -> dict[str, int]:
    leftovers: dict[str, int] = {}
    for service in DOWNSTREAM_SERVICES:
        for pod, rules in list_faults(service).items():
            if rules:
                leftovers[pod] = len(rules)
    return leftovers


def reset_circuit_breakers() -> None:
    for pod in running_pods("order-service"):
        _management(pod, "POST", "/internal/resilience/reset").raise_for_status()


def payment_authorizations(order_ids: list[str]) -> dict[str, dict]:
    pods = ready_pods("payment-service")
    if not pods or not order_ids:
        return {}
    summaries: dict[str, dict] = {}
    with portforward.forward(kube.NS_APP, f"pod/{pods[0]}", MANAGEMENT_PORT) as port:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            for order_id in order_ids:
                summaries[order_id] = client.get(f"/internal/payments/{order_id}").raise_for_status().json()
    return summaries


def delete_pod(workload: str, journal: Journal) -> str:
    namespace = WORKLOADS[workload][0]
    pods = ready_pods(workload)
    if not pods:
        raise RuntimeError(f"no ready pod of {workload} to delete")
    pod = pods[0]
    uid = kube.clients().core.read_namespaced_pod(pod, namespace).metadata.uid
    journal.record("delete-pod", workload=workload, pod=pod, uid=uid)
    kube.clients().core.delete_namespaced_pod(pod, namespace)
    return pod


def rollout_restart(workload: str, journal: Journal) -> None:
    namespace, kind = WORKLOADS[workload]
    journal.record("rollout-restart", workload=workload)
    stamp = dt.datetime.now(dt.UTC).isoformat()
    patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
    apps = kube.clients().apps
    if kind == "Deployment":
        apps.patch_namespaced_deployment(workload, namespace, patch)
    else:
        apps.patch_namespaced_stateful_set(workload, namespace, patch)


def current_replicas(workload: str) -> int:
    return int(_read_workload(workload).spec.replicas or 0)


def _set_replicas(workload: str, replicas: int) -> None:
    namespace, kind = WORKLOADS[workload]
    body = {"spec": {"replicas": replicas}}
    apps = kube.clients().apps
    if kind == "Deployment":
        apps.patch_namespaced_deployment_scale(workload, namespace, body)
    else:
        apps.patch_namespaced_stateful_set_scale(workload, namespace, body)


def scale(workload: str, replicas: int, journal: Journal) -> int:
    original = current_replicas(workload)
    journal.record("scale", workload=workload, originalReplicas=original, replicas=replicas)
    _set_replicas(workload, replicas)
    return original


def restore_replicas(workload: str, replicas: int) -> None:
    try:
        _set_replicas(workload, replicas)
    except ApiException as error:
        if error.status != 404:
            raise


def wait_rollout_complete(workload: str, timeout: float = 300) -> None:
    namespace, kind = WORKLOADS[workload]
    if kind == "Deployment":
        kube.wait_deployment_available(namespace, workload, timeout=timeout)
    else:
        kube.wait_statefulset_ready(namespace, workload, timeout=timeout)
