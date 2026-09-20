"""Creates the Kubernetes Secrets the platform needs, without ever writing values to Git.

Generated credentials are created only when the Secret does not exist yet, so running
bootstrap again never rotates them. TLS Secrets are updated when the local certificate
changes, which lets pods pick up renewed certificates.
"""

from __future__ import annotations

import base64
import secrets as pysecrets
import string
from collections.abc import Callable
from enum import StrEnum

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from txplatform import kube

_ALPHABET = string.ascii_letters + string.digits


class Outcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


def generate_secret_value(length: int = 32) -> str:
    return "".join(pysecrets.choice(_ALPHABET) for _ in range(length))


def _labels() -> dict[str, str]:
    return {"app.kubernetes.io/part-of": "txplatform", "app.kubernetes.io/managed-by": "platformctl"}


def ensure_generated_secret(namespace: str, name: str, keys: dict[str, Callable[[], str]]) -> Outcome:
    """Creates the Secret with generated values if it is missing; never changes existing values."""
    core = kube.clients().core
    try:
        core.read_namespaced_secret(name, namespace)
        return Outcome.UNCHANGED
    except ApiException as error:
        if error.status != 404:
            raise
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=_labels()),
        type="Opaque",
        string_data={key: factory() for key, factory in keys.items()},
    )
    core.create_namespaced_secret(namespace, body)
    return Outcome.CREATED


def apply_tls_secret(namespace: str, name: str, cert_pem: bytes, key_pem: bytes) -> Outcome:
    core = kube.clients().core
    data = {
        "tls.crt": base64.b64encode(cert_pem).decode(),
        "tls.key": base64.b64encode(key_pem).decode(),
    }
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=_labels()),
        type="kubernetes.io/tls",
        data=data,
    )
    try:
        existing = core.read_namespaced_secret(name, namespace)
    except ApiException as error:
        if error.status != 404:
            raise
        core.create_namespaced_secret(namespace, body)
        return Outcome.CREATED
    if existing.data == data:
        return Outcome.UNCHANGED
    core.replace_namespaced_secret(name, namespace, body)
    return Outcome.UPDATED


def read_value(namespace: str, name: str, key: str) -> str:
    secret = kube.clients().core.read_namespaced_secret(name, namespace)
    return base64.b64decode(secret.data[key]).decode()
