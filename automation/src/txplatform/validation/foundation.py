"""Foundation suite: proves the cluster itself behaves as the rest of the platform assumes.

It checks the pinned Kubernetes version, Pod Security levels, working volume provisioning,
real NetworkPolicy enforcement and loopback-only host ports. Enforcement is tested with
actual traffic because some local clusters accept NetworkPolicies without applying them.
"""

from __future__ import annotations

import time
import uuid

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from txplatform import kind, kube, probes, versions
from txplatform.validation.framework import CheckFailed, Suite, expect

EXPECTED_PSA = {
    kube.NS_GATEWAY: "restricted",
    kube.NS_APP: "restricted",
    kube.NS_OBSERVABILITY: "restricted",
}


def build() -> Suite:
    # A unique namespace per run avoids colliding with a previous run's namespace that is still being deleted.
    check_namespace = f"txplatform-check-{uuid.uuid4().hex[:6]}"
    created = False

    def ensure_check_namespace() -> None:
        nonlocal created
        if created:
            return
        kube.clients().core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(
            name=check_namespace, labels={"app.kubernetes.io/part-of": "txplatform", "txplatform.io/temporary": "true"},
        )))
        created = True

    def cleanup() -> None:
        if created:
            try:
                kube.clients().core.delete_namespace(check_namespace)
            except ApiException as error:
                if error.status != 404:
                    raise

    suite = Suite("foundation", "cluster version, pod security, storage, network policy enforcement, host ports", cleanup=cleanup)

    @suite.check("nodes ready on pinned Kubernetes version")
    def nodes() -> str:
        expected = versions.load().kubernetes.nodeImage.split(":")[1].split("@")[0]
        items = kube.clients().core.list_node().items
        expect(len(items) == 3, f"expected 3 nodes, found {len(items)}")
        for node in items:
            ready = any(c.type == "Ready" and c.status == "True" for c in node.status.conditions)
            expect(ready, f"node {node.metadata.name} is not Ready")
            kubelet = node.status.node_info.kubelet_version
            expect(kubelet == expected, f"node {node.metadata.name} runs {kubelet}, expected {expected}")
        return f"3 nodes on {expected}"

    @suite.check("namespaces carry Pod Security levels")
    def pod_security_labels() -> str:
        for namespace, level in EXPECTED_PSA.items():
            labels = kube.clients().core.read_namespace(namespace).metadata.labels or {}
            actual = labels.get("pod-security.kubernetes.io/enforce")
            expect(actual == level, f"{namespace} enforces '{actual}', expected '{level}'")
        return ", ".join(f"{ns}={lvl}" for ns, lvl in EXPECTED_PSA.items())

    @suite.check("platform namespaces reject a privileged pod")
    def pod_security_enforced() -> str:
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name="psa-check"),
            spec=client.V1PodSpec(containers=[client.V1Container(
                name="privileged", image=versions.load().images.probe,
                security_context=client.V1SecurityContext(privileged=True),
            )]),
        )
        for namespace in EXPECTED_PSA:
            try:
                # A server-side dry run is admitted (or rejected) exactly like a real create.
                kube.clients().core.create_namespaced_pod(namespace, pod, dry_run="All")
            except ApiException as error:
                expect(error.status == 403 and "violates PodSecurity" in (error.body or ""), f"{namespace}: unexpected rejection: {error.reason}")
                continue
            raise CheckFailed(f"privileged pod was admitted into {namespace}")
        return f"privileged pod rejected in {len(EXPECTED_PSA)} namespaces"

    @suite.check("persistent volume claim binds and stores data")
    def storage() -> str:
        ensure_check_namespace()
        core = kube.clients().core
        claim = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name="storage-check"),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1VolumeResourceRequirements(requests={"storage": "16Mi"}),
            ),
        )
        core.create_namespaced_persistent_volume_claim(check_namespace, claim)
        writer = client.V1Pod(
            metadata=client.V1ObjectMeta(name="storage-writer"),
            spec=client.V1PodSpec(
                restart_policy="Never",
                containers=[client.V1Container(
                    name="writer", image=versions.load().images.probe,
                    command=["sh", "-c", "echo stored > /data/check && cat /data/check"],
                    volume_mounts=[client.V1VolumeMount(name="data", mount_path="/data")],
                )],
                volumes=[client.V1Volume(name="data", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="storage-check"))],
            ),
        )
        core.create_namespaced_pod(check_namespace, writer)
        kube.wait_for(
            lambda: core.read_namespaced_pod("storage-writer", check_namespace).status.phase == "Succeeded",
            timeout=180, description="storage writer pod",
        )
        phase = core.read_namespaced_persistent_volume_claim("storage-check", check_namespace).status.phase
        expect(phase == "Bound", f"claim is {phase}")
        return "claim Bound and writable"

    @suite.check("NetworkPolicy is enforced for real traffic")
    def network_policy_enforced() -> str:
        ensure_check_namespace()
        core, networking = kube.clients().core, kube.clients().networking
        labels = {"txplatform.io/network-identity": "np-check-server"}
        server = client.V1Pod(
            metadata=client.V1ObjectMeta(name="np-server", labels=labels),
            spec=client.V1PodSpec(containers=[client.V1Container(
                name="server", image=versions.load().images.probe,
                command=["sh", "-c", "mkdir -p /www && echo ok > /www/index.html && httpd -f -p 8080 -h /www"],
                ports=[client.V1ContainerPort(container_port=8080)],
            )]),
        )
        core.create_namespaced_pod(check_namespace, server)
        core.create_namespaced_service(check_namespace, client.V1Service(
            metadata=client.V1ObjectMeta(name="np-server"),
            spec=client.V1ServiceSpec(selector=labels, ports=[client.V1ServicePort(port=8080, target_port=8080)]),
        ))
        kube.wait_for(lambda: kube.is_pod_ready(core.read_namespaced_pod("np-server", check_namespace)), timeout=120, description="np-server pod")
        target = probes.Target(f"np-server.{check_namespace}.svc.cluster.local", 8080)

        def reachable_becomes(expected: bool) -> bool:
            # New Services and policies take a few seconds to be programmed on every node, so the
            # probe is repeated until it shows the expected state or the time is up.
            deadline = time.monotonic() + 45
            while True:
                reachable = probes.run_tcp_probe(check_namespace, "np-check-client", [target])[str(target)]
                if reachable == expected or time.monotonic() > deadline:
                    return reachable == expected
                time.sleep(3)

        expect(reachable_becomes(True), "server was unreachable before any policy existed")
        networking.create_namespaced_network_policy(check_namespace, client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(name="deny-server-ingress"),
            spec=client.V1NetworkPolicySpec(pod_selector=client.V1LabelSelector(match_labels=labels), policy_types=["Ingress"]),
        ))
        expect(reachable_becomes(False), "server was still reachable 45 s after a deny-all ingress policy was created")
        return "reachable without policy, blocked with deny policy"

    @suite.check("platform containers declare their own resources and none was killed for memory")
    def resources() -> str:
        # Pod templates are inspected, not pods: Kubernetes copies the namespace defaults into pods
        # at admission, so a pod never shows whether its chart forgot to declare resources.
        apps, batch, core = kube.clients().apps, kube.clients().batch, kube.clients().core
        problems, containers = [], 0
        for namespace in EXPECTED_PSA:
            templates = [(f"deploy/{d.metadata.name}", d.spec.template) for d in apps.list_namespaced_deployment(namespace).items]
            templates += [(f"sts/{s.metadata.name}", s.spec.template) for s in apps.list_namespaced_stateful_set(namespace).items]
            templates += [(f"cronjob/{c.metadata.name}", c.spec.job_template.spec.template) for c in batch.list_namespaced_cron_job(namespace).items]
            for owner, template in templates:
                for container in [*(template.spec.init_containers or []), *template.spec.containers]:
                    containers += 1
                    resources = container.resources
                    if resources is None or not resources.requests or not resources.limits:
                        problems.append(f"{namespace}/{owner}/{container.name} declares no requests or limits")
            for pod in core.list_namespaced_pod(namespace).items:
                for status in pod.status.container_statuses or []:
                    terminated = status.last_state.terminated if status.last_state else None
                    if terminated is not None and terminated.reason == "OOMKilled":
                        problems.append(f"{namespace}/{pod.metadata.name}/{status.name} was OOM-killed")
        expect(not problems, "; ".join(problems))
        return f"{containers} containers declare requests and limits, no OOM kills"

    @suite.check("public host ports listen on loopback only")
    def host_ports() -> str:
        bindings = kind.host_port_bindings()
        expect(bindings, "control-plane container has no port bindings")
        exposed = [b for b in bindings if "->" in b and not b.split("->")[1].strip().startswith("127.0.0.1:")]
        # The API server binding is created by kind itself and also uses 127.0.0.1.
        expect(not exposed, f"ports bound beyond loopback: {exposed}")
        public = sorted(b for b in bindings if b.startswith(("30443/", "31443/")))
        expect(len(public) == 2, f"expected Kong and Keycloak port mappings, found {public}")
        return "; ".join(public)

    return suite
