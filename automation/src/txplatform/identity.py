"""Client-side access to the platform's public endpoints: TLS trust and OAuth tokens.

Both public endpoints (Kong on 8443, Keycloak on 9443) present certificates from the local CA,
so every HTTPS client here verifies against that CA instead of disabling verification.
Client secrets are read from the Kubernetes Secret at run time and never stored on disk.
"""

from __future__ import annotations

import base64
import json
import ssl
import time
from dataclasses import dataclass
from functools import cache

import httpx

from txplatform import kube, paths, secrets

REALM = "transaction-platform"
KEYCLOAK_PUBLIC_URL = "https://localhost:9443"
ISSUER = f"{KEYCLOAK_PUBLIC_URL}/realms/{REALM}"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
GATEWAY_URL = "https://localhost:8443"

CLIENT_SECRETS_NAME = "keycloak-clients"
ADMIN_SECRET_NAME = "keycloak-admin"
CLIENTS = ("scenario-runner", "order-reader")


@cache
def ca_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(paths.local_state_dir() / "pki" / "ca.crt"))


def https_client(base_url: str, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(base_url=base_url, verify=ca_context(), timeout=timeout)


def decode_claims(token: str) -> dict:
    """Reads a JWT payload without verifying it; only for inspecting what Keycloak issued."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


@dataclass
class _CachedToken:
    value: str
    expires_at: float


class TokenProvider:
    """Client-credentials tokens, reused until shortly before they expire."""

    def __init__(self) -> None:
        self._cache: dict[str, _CachedToken] = {}

    def token(self, client_id: str) -> str:
        cached = self._cache.get(client_id)
        if cached and cached.expires_at - time.time() > 30:
            return cached.value
        secret = secrets.read_value(kube.NS_APP, CLIENT_SECRETS_NAME, client_id)
        with httpx.Client(verify=ca_context(), timeout=30) as client:
            response = client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
            )
            response.raise_for_status()
        body = response.json()
        self._cache[client_id] = _CachedToken(body["access_token"], time.time() + int(body.get("expires_in", 60)))
        return body["access_token"]
