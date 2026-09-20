"""Placing orders through the public gateway, as a real client would.

Shared by validation suites and the scenario runner so every experiment exercises the same path:
TLS at Kong -> OrderService -> dependencies, with the trace id taken from the traceresponse header.
"""

from __future__ import annotations

import copy

import httpx

from txplatform import identity

NORMAL_ORDER = {"items": [{"sku": "SKU-1001", "quantity": 1}], "paymentToken": "tok_test_approved", "shippingZone": "domestic"}


def normal_order() -> dict:
    return copy.deepcopy(NORMAL_ORDER)


def trace_id_of(response: httpx.Response) -> str:
    """Trace id from the W3C traceresponse header (00-<trace id>-<span id>-<flags>), or ''."""
    parts = response.headers.get("traceresponse", "").split("-")
    return parts[1] if len(parts) == 4 else ""


def place(
    tokens: identity.TokenProvider,
    order: dict | None = None,
    *,
    client_id: str | None = "scenario-runner",
    headers: dict[str, str] | None = None,
    debug: bool = False,
) -> httpx.Response:
    request_headers = dict(headers or {})
    if client_id:
        request_headers["Authorization"] = f"Bearer {tokens.token(client_id)}"
    if debug:
        request_headers["X-Debug-Trace"] = "true"
    with identity.https_client(identity.GATEWAY_URL) as client:
        return client.post("/orders", json=order if order is not None else normal_order(), headers=request_headers)
