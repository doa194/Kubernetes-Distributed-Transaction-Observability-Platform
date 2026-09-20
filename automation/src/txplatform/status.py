"""Prints a compact overview of the running platform for operators."""

from __future__ import annotations

import datetime as dt

from cryptography import x509

from txplatform import console, helm, kind, kube, paths, runlock
from txplatform.scenarios import records


def show() -> bool:
    if not kind.cluster_exists():
        console.warn(f"kind cluster '{kube.CLUSTER_NAME}' does not exist; run `platformctl bootstrap`")
        return False
    kube.guard()
    api = kube.clients()

    console.step("Nodes")
    for node in api.core.list_node().items:
        ready = any(c.type == "Ready" and c.status == "True" for c in node.status.conditions)
        console.info(f"{node.metadata.name:<28} {'Ready' if ready else 'NotReady':<9} {node.status.node_info.kubelet_version}")

    console.step("Namespaces")
    for namespace in kube.PLATFORM_NAMESPACES:
        if not kube.namespace_exists(namespace):
            console.info(f"{namespace:<22} missing")
            continue
        labels = api.core.read_namespace(namespace).metadata.labels or {}
        console.info(f"{namespace:<22} pod-security={labels.get('pod-security.kubernetes.io/enforce', '-')}")

    console.step("Helm releases")
    for release in helm.list_releases():
        console.info(f"{release['name']:<24} {release['namespace']:<22} {release['status']:<10} {release['chart']}")

    console.step("Workloads")
    for namespace in kube.PLATFORM_NAMESPACES:
        if not kube.namespace_exists(namespace):
            continue
        for deployment in api.apps.list_namespaced_deployment(namespace).items:
            desired = deployment.spec.replicas or 0
            console.info(f"{namespace:<22} deploy/{deployment.metadata.name:<30} {deployment.status.ready_replicas or 0}/{desired} ready")
        for statefulset in api.apps.list_namespaced_stateful_set(namespace).items:
            desired = statefulset.spec.replicas or 0
            console.info(f"{namespace:<22} sts/{statefulset.metadata.name:<33} {statefulset.status.ready_replicas or 0}/{desired} ready")

    console.step("Experiments")
    lock = runlock.current() if kube.namespace_exists(kube.NS_APP) else None
    if lock is None:
        console.info("experiment lock: free")
    else:
        expired = " (expired, may be taken over)" if runlock.is_expired(lock, dt.datetime.now(dt.UTC)) else ""
        console.info(f"experiment lock: held for {lock.purpose} by {lock.holder}, renewed {lock.renewed_at:%H:%M:%S} UTC{expired}")
    unrestored = records.unrestored_runs()
    if unrestored:
        console.warn(f"runs that were not restored (run `scenarioctl reset`): {', '.join(r.run_id for r in unrestored)}")
    else:
        console.info("every recorded run was restored")

    cert_path = paths.local_state_dir() / "pki" / "tls.crt"
    if cert_path.is_file():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        days = (cert.not_valid_after_utc - dt.datetime.now(dt.UTC)).days
        console.step("Certificates")
        console.info(f"server certificate valid for {days} more days (CA: {paths.local_state_dir() / 'pki' / 'ca.crt'})")
    return True
