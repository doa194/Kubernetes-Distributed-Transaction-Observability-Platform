"""A small, typed model of traces returned by the Jaeger Query API (api_v3, OTLP JSON).

Every trace check in the automation works on this model instead of raw JSON, so the parsing
details (id encodings, attribute value types, enum spellings) live in exactly one place.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

_KIND_NAMES = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_STATUS_NAMES = {0: "UNSET", 1: "OK", 2: "ERROR"}


@dataclass(frozen=True)
class Event:
    name: str
    time_unix_nano: int
    attributes: dict[str, Any]


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    kind: str
    start_unix_nano: int
    end_unix_nano: int
    status: str
    attributes: dict[str, Any]
    events: tuple[Event, ...]
    resource: dict[str, Any]

    @property
    def service(self) -> str:
        return str(self.resource.get("service.name", ""))

    @property
    def duration_ms(self) -> float:
        return (self.end_unix_nano - self.start_unix_nano) / 1_000_000

    @property
    def is_root(self) -> bool:
        return not self.parent_span_id

    def overlaps(self, other: Span) -> bool:
        return self.start_unix_nano < other.end_unix_nano and other.start_unix_nano < self.end_unix_nano


@dataclass(frozen=True)
class Trace:
    trace_id: str
    spans: tuple[Span, ...] = field(default_factory=tuple)

    @property
    def services(self) -> set[str]:
        return {span.service for span in self.spans}

    def named(self, name: str) -> list[Span]:
        return [span for span in self.spans if span.name == name]

    def by_service(self, service: str, kind: str | None = None) -> list[Span]:
        return [span for span in self.spans if span.service == service and (kind is None or span.kind == kind)]

    def children_of(self, span: Span) -> list[Span]:
        return [child for child in self.spans if child.parent_span_id == span.span_id]

    def parent_of(self, span: Span) -> Span | None:
        return next((candidate for candidate in self.spans if candidate.span_id == span.parent_span_id), None)

    def roots(self) -> list[Span]:
        known = {span.span_id for span in self.spans}
        return [span for span in self.spans if not span.parent_span_id or span.parent_span_id not in known]


def normalize_id(value: str | None, byte_length: int) -> str:
    """Jaeger may encode ids as hex or as base64 bytes; both become lowercase hex."""
    if not value:
        return ""
    if len(value) == byte_length * 2 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower()
    raw = base64.b64decode(value + "=" * (-len(value) % 4))
    return raw.hex() if any(raw) else ""


def attribute_value(value: dict[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [attribute_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return attributes(value["kvlistValue"].get("values", []))
    return None


def attributes(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {item["key"]: attribute_value(item.get("value", {})) for item in items or []}


def _enum(value: Any, names: dict[int, str], prefix: str) -> str:
    if isinstance(value, int):
        return names.get(value, "UNSPECIFIED")
    text = str(value or "")
    return text.removeprefix(prefix) or names[0]


def _resource_spans(document: Any) -> list[dict[str, Any]]:
    """Accepts a TracesData object, a {"result": ...} wrapper, or a list of streamed chunks."""
    if isinstance(document, list):
        return [item for chunk in document for item in _resource_spans(chunk)]
    if isinstance(document, dict):
        if "result" in document:
            return _resource_spans(document["result"])
        return list(document.get("resourceSpans", []))
    return []


def parse_spans(document: Any) -> list[Span]:
    spans: list[Span] = []
    for resource_spans in _resource_spans(document):
        resource = attributes(resource_spans.get("resource", {}).get("attributes"))
        for scope_spans in resource_spans.get("scopeSpans", []):
            for raw in scope_spans.get("spans", []):
                spans.append(Span(
                    trace_id=normalize_id(raw.get("traceId"), 16),
                    span_id=normalize_id(raw.get("spanId"), 8),
                    parent_span_id=normalize_id(raw.get("parentSpanId"), 8),
                    name=raw.get("name", ""),
                    kind=_enum(raw.get("kind"), _KIND_NAMES, "SPAN_KIND_"),
                    start_unix_nano=int(raw.get("startTimeUnixNano", 0)),
                    end_unix_nano=int(raw.get("endTimeUnixNano", 0)),
                    status=_enum(raw.get("status", {}).get("code"), _STATUS_NAMES, "STATUS_CODE_"),
                    attributes=attributes(raw.get("attributes")),
                    events=tuple(
                        Event(e.get("name", ""), int(e.get("timeUnixNano", 0)), attributes(e.get("attributes")))
                        for e in raw.get("events", [])
                    ),
                    resource=resource,
                ))
    return spans


def group_traces(spans: list[Span]) -> list[Trace]:
    grouped: dict[str, list[Span]] = {}
    for span in spans:
        grouped.setdefault(span.trace_id, []).append(span)
    return [Trace(trace_id, tuple(sorted(items, key=lambda s: s.start_unix_nano))) for trace_id, items in grouped.items()]
