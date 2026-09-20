"""Well-known locations inside the repository.

The automation can be started from any working directory, so every path is resolved
from the repository root, which is found by walking up to `deploy/versions.yaml`.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path


@cache
def repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "deploy" / "versions.yaml").is_file():
            return candidate
    raise RuntimeError("Could not find the repository root (deploy/versions.yaml is missing).")


def deploy_dir() -> Path:
    return repo_root() / "deploy"


def charts_dir() -> Path:
    return deploy_dir() / "charts"


def local_values_dir() -> Path:
    return deploy_dir() / "values" / "local"


def local_state_dir() -> Path:
    """Generated, git-ignored material such as the local CA and certificates."""
    return repo_root() / ".local"


def runs_dir() -> Path:
    """Git-ignored scenario run records and journals."""
    return repo_root() / ".runs"


def project_tools_dir() -> Path:
    """Optional git-ignored folder with tool binaries downloaded for this workstation."""
    return repo_root() / ".tools" / "bin"


def scenarios_dir() -> Path:
    return repo_root() / "scenarios"
