"""Client for the Jaeger Query API (api_v3 over HTTP), reached through a port-forward."""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from txplatform import kube, portforward, traces

QUERY_SERVICE = "svc/jaeger-query"
QUERY_PORT = 16686


class JaegerClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def get_trace(self, trace_id: str) -> traces.Trace | None:
        response = self._http.get(f"/api/v3/traces/{trace_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        found = traces.group_traces(traces.parse_spans(_json_documents(response.text)))
        return next((t for t in found if t.trace_id == trace_id), None)

    def wait_for_trace(self, trace_id: str, *, timeout: float, until=None, interval: float = 2.0) -> traces.Trace | None:
        """Polls until the trace exists (and, if given, `until(trace)` is true) or the timeout passes."""
        deadline = time.monotonic() + timeout
        latest: traces.Trace | None = None
        while time.monotonic() < deadline:
            latest = self.get_trace(trace_id)
            if latest is not None and (until is None or until(latest)):
                return latest
            time.sleep(interval)
        return latest

    def find_traces(
        self,
        service: str,
        start: dt.datetime,
        end: dt.datetime,
        *,
        attributes: dict[str, str] | None = None,
        operation: str | None = None,
        limit: int = 100,
    ) -> list[traces.Trace]:
        # Jaeger 2.21 parameter names; attributes are a JSON object and must all match one span.
        params: dict[str, str] = {
            "query.serviceName": service,
            "query.startTimeMin": _rfc3339(start),
            "query.startTimeMax": _rfc3339(end),
            "query.searchDepth": str(limit),
        }
        if operation:
            params["query.operationName"] = operation
        if attributes:
            params["query.attributes"] = json.dumps(attributes)
        response = self._http.get("/api/v3/traces", params=params)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return traces.group_traces(traces.parse_spans(_json_documents(response.text)))

    def services(self) -> list[str]:
        response = self._http.get("/api/v3/services")
        response.raise_for_status()
        return list(response.json().get("services", []))

    def operations(self, service: str) -> list[str]:
        response = self._http.get("/api/v3/operations", params={"service": service})
        response.raise_for_status()
        return [op.get("name", "") for op in response.json().get("operations", [])]


def _rfc3339(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _json_documents(text: str) -> list:
    """The trace endpoints may stream several JSON objects one after another."""
    decoder = json.JSONDecoder()
    documents, index = [], 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        document, index = decoder.raw_decode(text, index)
        documents.append(document)
    return documents


@contextmanager
def connect() -> Iterator[JaegerClient]:
    with portforward.forward(kube.NS_OBSERVABILITY, QUERY_SERVICE, QUERY_PORT) as port:
        client = JaegerClient(f"http://127.0.0.1:{port}")
        try:
            yield client
        finally:
            client.close()
