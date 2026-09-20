"""Builds and sends synthetic OTLP/HTTP JSON spans straight to the Jaeger Collector.

Synthetic spans test the Collector pipeline in isolation: their status, duration, attributes and
arrival time are chosen exactly, which real traffic cannot guarantee. They use the service name
`pipeline-probe`, so they never mix with the metrics of real services.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from txplatform import kube, portforward

PROBE_SERVICE = "pipeline-probe"
SPAN_KIND_SERVER = 2
STATUS_ERROR = 2


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def _attribute(key: str, value: Any) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


@dataclass
class SyntheticSpan:
    name: str
    trace_id: str = field(default_factory=new_trace_id)
    span_id: str = field(default_factory=new_span_id)
    parent_span_id: str = ""
    duration_ms: float = 20
    error: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)
    service: str = PROBE_SERVICE
    kind: int = SPAN_KIND_SERVER
    end_unix_nano: int = 0

    def to_otlp(self) -> dict:
        end = self.end_unix_nano or time.time_ns()
        span = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(end - int(self.duration_ms * 1_000_000)),
            "endTimeUnixNano": str(end),
            "attributes": [_attribute(k, v) for k, v in self.attributes.items()],
        }
        if self.error:
            span["status"] = {"code": STATUS_ERROR, "message": "synthetic error"}
        return span


def build_payload(spans: list[SyntheticSpan]) -> dict:
    """Groups spans by service into one OTLP TracesData document."""
    by_service: dict[str, list[dict]] = {}
    for span in spans:
        by_service.setdefault(span.service, []).append(span.to_otlp())
    return {"resourceSpans": [
        {
            "resource": {"attributes": [_attribute("service.name", service)]},
            "scopeSpans": [{"scope": {"name": "txplatform.pipeline-probe"}, "spans": items}],
        }
        for service, items in by_service.items()
    ]}


class OtlpSender:
    def __init__(self, base_url: str) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=30)

    def send(self, spans: list[SyntheticSpan], batch_size: int = 200) -> None:
        for start in range(0, len(spans), batch_size):
            response = self._http.post("/v1/traces", json=build_payload(spans[start:start + batch_size]))
            response.raise_for_status()

    def close(self) -> None:
        self._http.close()


@contextmanager
def connect() -> Iterator[OtlpSender]:
    """Operator access to the Collector's cluster-internal OTLP/HTTP port through a port-forward."""
    with portforward.forward(kube.NS_OBSERVABILITY, "svc/jaeger-collector", 4318) as port:
        sender = OtlpSender(f"http://127.0.0.1:{port}")
        try:
            yield sender
        finally:
            sender.close()
