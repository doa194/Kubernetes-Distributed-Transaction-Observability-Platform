"""Checks that this workstation can run the platform before anything is created.

The evaluation functions are pure (facts in, findings out) so the thresholds can be unit
tested; `run()` gathers the real facts from the installed tools.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import sys
from dataclasses import dataclass
from enum import StrEnum

from txplatform import kind, paths, tools, versions

GIB = 1024**3
MIN_CPUS, RECOMMENDED_CPUS = 4, 6
MIN_MEMORY_GIB, RECOMMENDED_MEMORY_GIB = 8, 10
MIN_FREE_DISK_GIB = 30
PUBLIC_PORTS = (8443, 9443)


class Level(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Finding:
    level: Level
    check: str
    message: str


def evaluate_docker_resources(cpus: int, memory_bytes: int) -> list[Finding]:
    memory_gib = memory_bytes / GIB
    findings = []
    if cpus < MIN_CPUS:
        findings.append(Finding(Level.FAIL, "docker-cpus", f"{cpus} CPUs allocated; at least {MIN_CPUS} are required"))
    elif cpus < RECOMMENDED_CPUS:
        findings.append(Finding(Level.WARN, "docker-cpus", f"{cpus} CPUs allocated; {RECOMMENDED_CPUS} are recommended"))
    else:
        findings.append(Finding(Level.PASS, "docker-cpus", f"{cpus} CPUs allocated"))
    # Docker reports slightly less than the configured amount, so allow a small margin.
    if memory_gib < MIN_MEMORY_GIB * 0.95:
        findings.append(Finding(Level.FAIL, "docker-memory", f"{memory_gib:.1f} GiB allocated; at least {MIN_MEMORY_GIB} GiB required"))
    elif memory_gib < RECOMMENDED_MEMORY_GIB * 0.95:
        findings.append(Finding(Level.WARN, "docker-memory", f"{memory_gib:.1f} GiB allocated; {RECOMMENDED_MEMORY_GIB} GiB recommended"))
    else:
        findings.append(Finding(Level.PASS, "docker-memory", f"{memory_gib:.1f} GiB allocated"))
    return findings


def evaluate_disk(free_bytes: int) -> Finding:
    free_gib = free_bytes / GIB
    if free_gib < MIN_FREE_DISK_GIB:
        return Finding(Level.WARN, "disk", f"{free_gib:.0f} GiB free; {MIN_FREE_DISK_GIB} GiB recommended for images and volumes")
    return Finding(Level.PASS, "disk", f"{free_gib:.0f} GiB free")


def evaluate_version_prefix(check: str, actual: str | None, expected_prefix: str) -> Finding:
    if actual is None:
        return Finding(Level.FAIL, check, "not installed")
    if actual.startswith(expected_prefix):
        return Finding(Level.PASS, check, actual)
    return Finding(Level.WARN, check, f"{actual} found; {expected_prefix}* is the tested version")


def evaluate_kubectl_minor(client_minor: int | None, cluster_minor: int) -> Finding:
    if client_minor is None:
        return Finding(Level.FAIL, "kubectl", "not installed")
    # Kubernetes supports kubectl within one minor version of the API server.
    if abs(client_minor - cluster_minor) <= 1:
        return Finding(Level.PASS, "kubectl", f"client 1.{client_minor}")
    return Finding(Level.FAIL, "kubectl", f"client 1.{client_minor} is more than one minor version away from 1.{cluster_minor}")


def evaluate_python(version: tuple[int, int], minimum: str) -> Finding:
    required = tuple(int(part) for part in minimum.split("."))
    text = f"{version[0]}.{version[1]}"
    if version >= required:
        return Finding(Level.PASS, "python", text)
    return Finding(Level.FAIL, "python", f"{text} found; {minimum}+ required")


def evaluate_port(port: int, in_use: bool, cluster_exists: bool) -> Finding:
    if not in_use:
        return Finding(Level.PASS, f"port-{port}", "free")
    if cluster_exists:
        return Finding(Level.PASS, f"port-{port}", "bound by the existing project cluster")
    return Finding(Level.FAIL, f"port-{port}", "already in use by another program")


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _tool_output(tool: str, *args: str) -> str | None:
    if tools.find_tool(tool) is None:
        return None
    result = tools.run(tool, *args, check=False, timeout=60)
    return result.stdout.strip() if result.returncode == 0 else None


def run() -> list[Finding]:
    manifest = versions.load()
    findings: list[Finding] = []

    docker_info = _tool_output("docker", "info", "--format", "{{json .}}")
    if docker_info is None:
        findings.append(Finding(Level.FAIL, "docker", "Docker is not installed or not running"))
    else:
        info = json.loads(docker_info)
        findings.append(Finding(Level.PASS, "docker", f"server {info.get('ServerVersion')}"))
        findings.extend(evaluate_docker_resources(int(info.get("NCPU", 0)), int(info.get("MemTotal", 0))))

    kind_version = _tool_output("kind", "version")
    findings.append(evaluate_version_prefix("kind", kind_version.split()[1] if kind_version else None, manifest.tools.kind))

    helm_version = _tool_output("helm", "version", "--short")
    findings.append(evaluate_version_prefix("helm", helm_version, manifest.tools.helm))

    kubectl_json = _tool_output("kubectl", "version", "--client", "-o", "json")
    client_minor = None
    if kubectl_json:
        minor_text = json.loads(kubectl_json)["clientVersion"]["minor"]
        client_minor = int(re.sub(r"\D", "", minor_text))
    findings.append(evaluate_kubectl_minor(client_minor, int(manifest.tools.kubectlMinor.split(".")[1])))

    sdks = _tool_output("dotnet", "--list-sdks") or ""
    sdk_versions = [line.split()[0] for line in sdks.splitlines() if line.strip()]
    stable = [v for v in sdk_versions if v.startswith(f"{manifest.tools.dotnetSdkMajor}.") and "-" not in v]
    findings.append(
        Finding(Level.PASS, "dotnet-sdk", stable[-1])
        if stable
        else Finding(Level.FAIL, "dotnet-sdk", f".NET SDK {manifest.tools.dotnetSdkMajor} is not installed")
    )

    findings.append(evaluate_python(sys.version_info[:2], manifest.tools.pythonMinimum))
    findings.append(evaluate_disk(shutil.disk_usage(paths.repo_root()).free))

    cluster_exists = docker_info is not None and kind_version is not None and kind.cluster_exists()
    for port in PUBLIC_PORTS:
        findings.append(evaluate_port(port, _port_in_use(port), cluster_exists))
    return findings
