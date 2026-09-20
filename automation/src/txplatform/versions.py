"""Loads and validates deploy/versions.yaml, the single pinned-version manifest.

Validation happens up front so a typo in a version pin fails immediately with a clear
message instead of surfacing later as a confusing kind, Helm or Docker error.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from txplatform import paths


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolVersions(_Strict):
    kind: str
    helm: str
    kubectlMinor: str
    dotnetSdkMajor: str
    pythonMinimum: str


class KubernetesVersions(_Strict):
    nodeImage: str

    @field_validator("nodeImage")
    @classmethod
    def require_digest(cls, value: str) -> str:
        # Kind only guarantees a compatible node image when it is pinned by digest.
        if "@sha256:" not in value:
            raise ValueError("kubernetes.nodeImage must be pinned with an @sha256 digest")
        return value


class GatewayApiVersions(_Strict):
    version: str
    standardCrds: str


class ChartPin(_Strict):
    repository: str
    chart: str
    version: str


class ChartVersions(_Strict):
    kong: ChartPin
    opensearch: ChartPin
    prometheus: ChartPin


class ImageVersions(_Strict):
    kongGateway: str
    kongIngressController: str
    keycloak: str
    jaeger: str
    jaegerIndexCleaner: str
    opensearch: str
    prometheus: str
    probe: str
    dotnetSdk: str
    dotnetRuntime: str

    @field_validator("*")
    @classmethod
    def require_tag(cls, value: str) -> str:
        name = value.rsplit("/", 1)[-1]
        if ":" not in name and "@" not in name:
            raise ValueError(f"image '{value}' must be pinned with a tag or digest")
        if name.endswith(":latest"):
            raise ValueError(f"image '{value}' must not use the floating 'latest' tag")
        return value


class VersionManifest(_Strict):
    schemaVersion: int
    tools: ToolVersions
    kubernetes: KubernetesVersions
    gatewayApi: GatewayApiVersions
    charts: ChartVersions
    images: ImageVersions


def parse_manifest(text: str) -> VersionManifest:
    return VersionManifest.model_validate(yaml.safe_load(text))


@cache
def load() -> VersionManifest:
    manifest_path: Path = paths.deploy_dir() / "versions.yaml"
    return parse_manifest(manifest_path.read_text(encoding="utf-8"))


def split_image(image: str) -> tuple[str, str]:
    """Splits `repository:tag` into its parts for Helm values that expect them separately."""
    repository, _, tag = image.rpartition(":")
    return repository, tag
