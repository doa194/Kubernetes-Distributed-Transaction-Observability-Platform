"""The version manifest must reject pins that would make the platform non-reproducible."""

import pytest
import yaml
from pydantic import ValidationError

from txplatform import paths, versions


def _manifest_dict() -> dict:
    return yaml.safe_load((paths.deploy_dir() / "versions.yaml").read_text(encoding="utf-8"))


def test_repository_manifest_is_valid():
    manifest = versions.load()

    assert "@sha256:" in manifest.kubernetes.nodeImage
    assert manifest.images.kongGateway == "kong:3.9.3"


def test_node_image_without_digest_is_rejected():
    data = _manifest_dict()
    data["kubernetes"]["nodeImage"] = "kindest/node:v1.36.4"

    with pytest.raises(ValidationError, match="digest"):
        versions.parse_manifest(yaml.safe_dump(data))


@pytest.mark.parametrize("image", ["quay.io/keycloak/keycloak", "jaegertracing/jaeger:latest"])
def test_image_without_fixed_tag_is_rejected(image):
    data = _manifest_dict()
    data["images"]["jaeger"] = image

    with pytest.raises(ValidationError):
        versions.parse_manifest(yaml.safe_dump(data))


def test_unknown_keys_are_rejected_to_catch_typos():
    data = _manifest_dict()
    data["charts"]["kong"]["verison"] = "1.0.0"

    with pytest.raises(ValidationError):
        versions.parse_manifest(yaml.safe_dump(data))


def test_split_image_handles_registry_ports():
    assert versions.split_image("quay.io/prometheus/prometheus:v3.14.0") == ("quay.io/prometheus/prometheus", "v3.14.0")
