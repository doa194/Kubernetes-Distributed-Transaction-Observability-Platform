"""Short-lived probe pods that test real TCP connectivity from a chosen network identity.

NetworkPolicies select pods by the `txplatform.io/network-identity` label. Probe pods carry
that label but none of the workload labels, so Services and ReplicaSets never mistake a
probe for a real pod while it tests what that identity is allowed to reach.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from txplatform import kube, versions

IDENTITY_LABEL = "txplatform.io/network-identity"


@dataclass(frozen=True)
class Target:
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def _restricted_security_context() -> tuple[client.V1PodSecurityContext, client.V1SecurityContext]:
    pod = client.V1PodSecurityContext(
        run_as_non_root=True, run_as_user=65534, run_as_group=65534,
        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
    )
    container = client.V1SecurityContext(
        allow_privilege_escalation=False, read_only_root_filesystem=True,
        capabilities=client.V1Capabilities(drop=["ALL"]),
    )
    return pod, container


def build_probe_script(targets: list[Target], connect_timeout: int) -> str:
    lines = [
        f'if nc -z -w {connect_timeout} {t.host} {t.port}; then echo "PROBE OK {t}"; else echo "PROBE BLOCKED {t}"; fi'
        for t in targets
    ]
    return "\n".join(lines)


def parse_probe_output(output: str) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[0] == "PROBE" and parts[1] in {"OK", "BLOCKED"}:
            results[parts[2]] = parts[1] == "OK"
    return results


def run_tcp_probe(
    namespace: str,
    identity: str | None,
    targets: list[Target],
    *,
    extra_labels: dict[str, str] | None = None,
    connect_timeout: int = 3,
) -> dict[str, bool]:
    """Starts one pod that tries every target and returns reachability per `host:port`."""
    core = kube.clients().core
    name = f"netprobe-{uuid.uuid4().hex[:8]}"
    labels = {"app.kubernetes.io/part-of": "txplatform", "txplatform.io/probe": "true", **(extra_labels or {})}
    if identity:
        labels[IDENTITY_LABEL] = identity
    pod_security, container_security = _restricted_security_context()
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=client.V1PodSpec(
            restart_policy="Never",
            automount_service_account_token=False,
            security_context=pod_security,
            containers=[
                client.V1Container(
                    name="probe",
                    image=versions.load().images.probe,
                    command=["sh", "-c", build_probe_script(targets, connect_timeout)],
                    security_context=container_security,
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "10m", "memory": "8Mi"}, limits={"cpu": "100m", "memory": "32Mi"}
                    ),
                )
            ],
        ),
    )
    core.create_namespaced_pod(namespace, pod)
    try:
        deadline = 90 + len(targets) * (connect_timeout + 2)

        def finished() -> bool:
            phase = core.read_namespaced_pod(name, namespace).status.phase
            return phase in {"Succeeded", "Failed"}

        kube.wait_for(finished, timeout=deadline, interval=1.0, description=f"probe pod {namespace}/{name}")
        logs = kube.pod_logs(namespace, name)
        results = parse_probe_output(logs)
        missing = [str(t) for t in targets if str(t) not in results]
        if missing:
            raise RuntimeError(f"probe pod {name} did not report on {missing}; output: {logs[-500:]}")
        return results
    finally:
        try:
            core.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
        except ApiException:
            pass


def build_http_probe_script(urls: list[str], timeout: int) -> str:
    lines = []
    for index, url in enumerate(urls):
        lines.append(f'echo "PROBE-HTTP-BEGIN {index}"')
        lines.append(f'wget -q -O - -T {timeout} "{url}"; echo; echo "PROBE-HTTP-END {index} $?"')
    return "\n".join(lines)


def parse_http_probe_output(output: str, count: int) -> list[tuple[bool, str]]:
    """Returns (succeeded, body) per URL in the order they were probed."""
    results: list[tuple[bool, str]] = []
    for index in range(count):
        begin = output.find(f"PROBE-HTTP-BEGIN {index}\n")
        end_marker = f"PROBE-HTTP-END {index} "
        end = output.find(end_marker, begin)
        if begin < 0 or end < 0:
            results.append((False, ""))
            continue
        body = output[begin + len(f"PROBE-HTTP-BEGIN {index}\n"):end].strip()
        exit_code = output[end + len(end_marker):].split("\n", 1)[0].strip()
        results.append((exit_code == "0", body))
    return results


def run_http_probe(namespace: str, identity: str | None, urls: list[str], *, timeout: int = 5) -> list[tuple[bool, str]]:
    """Fetches each URL from inside the cluster as the given network identity."""
    core = kube.clients().core
    name = f"httpprobe-{uuid.uuid4().hex[:8]}"
    labels = {"app.kubernetes.io/part-of": "txplatform", "txplatform.io/probe": "true"}
    if identity:
        labels[IDENTITY_LABEL] = identity
    pod_security, container_security = _restricted_security_context()
    core.create_namespaced_pod(namespace, client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=client.V1PodSpec(
            restart_policy="Never",
            automount_service_account_token=False,
            security_context=pod_security,
            containers=[client.V1Container(
                name="probe",
                image=versions.load().images.probe,
                command=["sh", "-c", build_http_probe_script(urls, timeout)],
                security_context=container_security,
                resources=client.V1ResourceRequirements(requests={"cpu": "10m", "memory": "8Mi"}, limits={"cpu": "100m", "memory": "32Mi"}),
            )],
        ),
    ))
    try:
        kube.wait_for(
            lambda: core.read_namespaced_pod(name, namespace).status.phase in {"Succeeded", "Failed"},
            timeout=90 + len(urls) * (timeout + 2), interval=1.0, description=f"http probe pod {namespace}/{name}",
        )
        return parse_http_probe_output(kube.pod_logs(namespace, name), len(urls))
    finally:
        try:
            core.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
        except ApiException:
            pass


def wait_until_gone(namespace: str, label_selector: str, timeout: float = 60) -> None:
    core = kube.clients().core
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not core.list_namespaced_pod(namespace, label_selector=label_selector).items:
            return
        time.sleep(1)
