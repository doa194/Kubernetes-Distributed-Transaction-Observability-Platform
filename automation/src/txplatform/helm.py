"""Thin wrapper around the Helm CLI, always pinned to the project's kube context."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from txplatform import console, kube, tools

PENDING_STATES = {"pending-install", "pending-upgrade", "pending-rollback"}
# Longer than the longest --timeout the automation passes to Helm: an operation that is still
# pending after this time cannot belong to a Helm process that is still running.
STALE_PENDING_AFTER = dt.timedelta(minutes=20)


def _helm(*args: str, timeout: float = 1200, check: bool = True) -> tools.CommandResult:
    return tools.run("helm", "--kube-context", kube.CONTEXT, *args, timeout=timeout, check=check)


def ensure_repo(name: str, url: str) -> None:
    listed = tools.run("helm", "repo", "list", "-o", "json", check=False)
    existing = {entry["name"]: entry["url"] for entry in json.loads(listed.stdout or "[]")} if listed.returncode == 0 else {}
    if existing.get(name) != url:
        tools.run("helm", "repo", "add", name, url, "--force-update", timeout=300)
    tools.run("helm", "repo", "update", name, timeout=300)


def upgrade_install(
    release: str,
    chart: str,
    namespace: str,
    *,
    version: str | None = None,
    values_files: list[Path] | None = None,
    set_values: dict[str, str] | None = None,
    wait: bool = True,
    timeout: str = "10m",
) -> None:
    recover_interrupted(release, namespace)
    args = ["upgrade", "--install", release, chart, "--namespace", namespace]
    if version:
        args += ["--version", version]
    for values_file in values_files or []:
        args += ["--values", str(values_file)]
    for key, value in (set_values or {}).items():
        args += ["--set-string", f"{key}={value}"]
    if wait:
        args += ["--wait", "--timeout", timeout]
    _helm(*args)


def parse_helm_time(value: str) -> dt.datetime:
    """Helm prints nanoseconds; Python's datetime keeps microseconds."""
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", value.strip())
    return dt.datetime.fromisoformat(trimmed.replace("Z", "+00:00"))


def pending_release_action(info: dict, now: dt.datetime) -> str:
    """What to do with a release before upgrading it: "proceed", "wait" (a deployment may still be
    running), "uninstall" (a stale interrupted first install) or "rollback" (a stale interrupted change)."""
    if info["status"] not in PENDING_STATES:
        return "proceed"
    if now - parse_helm_time(info["last_deployed"]) < STALE_PENDING_AFTER:
        return "wait"
    return "uninstall" if info["status"] == "pending-install" else "rollback"


def recover_interrupted(release: str, namespace: str) -> None:
    """A killed `helm upgrade` leaves its release in a pending state, and Helm then refuses every
    later operation on it. A stale pending first install is removed (persistent volumes stay);
    a stale pending upgrade or rollback is rolled back to the last deployed revision."""
    result = _helm("status", release, "--namespace", namespace, "-o", "json", check=False)
    if result.returncode != 0:
        return
    info = json.loads(result.stdout)["info"]
    action = pending_release_action(info, dt.datetime.now(dt.UTC))
    if action == "wait":
        raise tools.ToolError(
            f"release {namespace}/{release} is {info['status']} since {info['last_deployed']}; another deployment may still "
            f"be running. Wait for it; after {int(STALE_PENDING_AFTER.total_seconds() // 60)} minutes the release is recovered automatically."
        )
    if action == "uninstall":
        uninstall(release, namespace)
    elif action == "rollback":
        _helm("rollback", release, "--namespace", namespace, "--wait", "--timeout", "10m")
    if action != "proceed":
        console.warn(f"release {namespace}/{release} was left {info['status']} by an interrupted deployment: {action} done")


def uninstall(release: str, namespace: str) -> None:
    _helm("uninstall", release, "--namespace", namespace, "--wait", check=False)


def list_releases() -> list[dict[str, str]]:
    result = _helm("list", "--all-namespaces", "-o", "json", check=False)
    return json.loads(result.stdout or "[]") if result.returncode == 0 else []


def get_values(release: str, namespace: str) -> str:
    """All values of a release (user-supplied and computed), as YAML text."""
    return _helm("get", "values", release, "--namespace", namespace, "--all", "-o", "yaml", check=False).stdout


def template(chart: str, namespace: str, *, values_files: list[Path] | None = None, set_values: dict[str, str] | None = None) -> str:
    args = ["template", "render-check", chart, "--namespace", namespace]
    for values_file in values_files or []:
        args += ["--values", str(values_file)]
    for key, value in (set_values or {}).items():
        args += ["--set-string", f"{key}={value}"]
    return tools.run("helm", *args, timeout=120).stdout
