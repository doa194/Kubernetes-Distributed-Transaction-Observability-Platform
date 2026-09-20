"""Preflight thresholds decide whether bootstrap may start on this workstation."""

import pytest

from txplatform.preflight import GIB, Level, evaluate_disk, evaluate_docker_resources, evaluate_kubectl_minor, evaluate_port, evaluate_python, evaluate_version_prefix


def _levels(findings):
    return {f.check: f.level for f in findings}


@pytest.mark.parametrize(
    ("cpus", "memory_gib", "expected"),
    [
        (12, 13.6, {"docker-cpus": Level.PASS, "docker-memory": Level.PASS}),
        (4, 8.0, {"docker-cpus": Level.WARN, "docker-memory": Level.WARN}),
        (2, 6.0, {"docker-cpus": Level.FAIL, "docker-memory": Level.FAIL}),
    ],
)
def test_docker_resources(cpus, memory_gib, expected):
    assert _levels(evaluate_docker_resources(cpus, int(memory_gib * GIB))) == expected


def test_low_disk_space_only_warns():
    assert evaluate_disk(10 * GIB).level is Level.WARN


@pytest.mark.parametrize(("client_minor", "level"), [(36, Level.PASS), (37, Level.PASS), (34, Level.FAIL), (None, Level.FAIL)])
def test_kubectl_version_skew(client_minor, level):
    assert evaluate_kubectl_minor(client_minor, 36).level is level


def test_untested_tool_version_warns_and_missing_tool_fails():
    assert evaluate_version_prefix("helm", "v3.22.0+g1", "v4").level is Level.WARN
    assert evaluate_version_prefix("helm", None, "v4").level is Level.FAIL


def test_python_minimum():
    assert evaluate_python((3, 14), "3.12").level is Level.PASS
    assert evaluate_python((3, 11), "3.12").level is Level.FAIL


def test_busy_port_is_fine_only_when_the_project_cluster_owns_it():
    assert evaluate_port(8443, in_use=True, cluster_exists=True).level is Level.PASS
    assert evaluate_port(8443, in_use=True, cluster_exists=False).level is Level.FAIL
