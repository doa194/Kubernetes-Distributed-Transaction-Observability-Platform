"""Builds the .NET service images and tags them with a hash of their source.

A content-based tag means an unchanged service keeps the same tag, so repeated deployments
neither rebuild nor restart it, while any source change produces a new tag and a rollout.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from txplatform import console, paths, tools, versions

# Kubernetes service name -> .NET project folder under src/
SERVICES: dict[str, str] = {
    "order-service": "OrderService",
    "inventory-service": "InventoryService",
    "fraud-service": "FraudService",
    "payment-service": "PaymentService",
    "shipping-service": "ShippingService",
}

REPOSITORY = "txplatform"
_SHARED_INPUTS = ("global.json", "Directory.Build.props", "Directory.Packages.props", ".dockerignore", "src/Dockerfile")
_IGNORED_DIRECTORIES = {"bin", "obj"}


def _source_files(folder: Path) -> Iterable[Path]:
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not _IGNORED_DIRECTORIES.intersection(path.relative_to(folder).parts):
            yield path


def content_tag(project: str, root: Path | None = None) -> str:
    """Hash of every file that goes into the image. Line endings are normalized so a Windows
    checkout and a Linux checkout of the same commit produce the same tag."""
    root = root or paths.repo_root()
    project_folder = root / "src" / project
    # Without this check a renamed or misspelled project would hash to a valid-looking tag of
    # shared files only, and every service would silently get the same image.
    if not project_folder.is_dir():
        raise tools.ToolError(f"no .NET project folder at {project_folder}")
    digest = hashlib.sha256()
    files = [root / name for name in _SHARED_INPUTS]
    files += list(_source_files(root / "src" / "ServiceDefaults"))
    files += list(_source_files(project_folder))
    for file in files:
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(file.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]


def image_name(service: str, tag: str) -> str:
    return f"{REPOSITORY}/{service}:{tag}"


def _exists_locally(image: str) -> bool:
    return tools.run("docker", "image", "inspect", image, check=False, timeout=60).returncode == 0


def _build(service: str, project: str, tag: str) -> None:
    manifest = versions.load()
    image = image_name(service, tag)
    console.info(f"building {image}")
    tools.run(
        "docker", "build",
        "--file", str(paths.repo_root() / "src" / "Dockerfile"),
        "--build-arg", f"SERVICE={project}",
        "--build-arg", f"SDK_IMAGE={manifest.images.dotnetSdk}",
        "--build-arg", f"RUNTIME_IMAGE={manifest.images.dotnetRuntime}",
        "--tag", image,
        str(paths.repo_root()),
        timeout=1800,
    )


def ensure_service_images() -> dict[str, str]:
    """Builds only images whose content tag is not present yet; returns the tag per service."""
    tags = {service: content_tag(project) for service, project in SERVICES.items()}
    missing = [(s, SERVICES[s], t) for s, t in tags.items() if not _exists_locally(image_name(s, t))]
    for service, tag in tags.items():
        if service not in {m[0] for m in missing}:
            console.info(f"{image_name(service, tag)} is up to date")
    # Builds run in parallel; Docker shares the base image layers between them.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in [pool.submit(_build, *item) for item in missing]:
            future.result()
    return tags
