"""The trace model must read Jaeger's api_v3 output exactly, whatever id or enum encoding it uses."""

import base64

from txplatform import traces
from txplatform.validation.tracing import trace_id_from_traceresponse

TRACE_ID = "60a34d4828a46b1e9747fb35905f9cdc"


def _span(span_id: str, parent: str, name: str, kind, start: int, end: int, status=None, attributes=None, events=None) -> dict:
    return {
        "traceId": TRACE_ID, "spanId": span_id, "parentSpanId": parent, "name": name, "kind": kind,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(end), "status": status or {},
        "attributes": attributes or [], "events": events or [],
    }


def _document() -> dict:
    """Shaped like a real Jaeger 2.21 response: a result wrapper, hex ids, numeric kinds."""
    return {"result": {"resourceSpans": [
        {
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "order-service"}},
                {"key": "k8s.pod.name", "value": {"stringValue": "order-service-abc"}},
            ]},
            "scopeSpans": [{"spans": [
                _span("a1a1a1a1a1a1a1a1", "", "POST /orders", 2, 1_000, 9_000),
                _span("b2b2b2b2b2b2b2b2", "a1a1a1a1a1a1a1a1", "order.transaction", 1, 2_000, 8_000,
                      status={"code": 2, "message": "payment timeout"},
                      attributes=[{"key": "retry.count", "value": {"intValue": "2"}}, {"key": "sampling.debug", "value": {"boolValue": True}}],
                      events=[{"name": "order.state_changed", "timeUnixNano": "3000", "attributes": [{"key": "order.state.to", "value": {"stringValue": "InventoryReserved"}}]}]),
            ]}],
        },
        {
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "payment-service"}}]},
            "scopeSpans": [{"spans": [_span("c3c3c3c3c3c3c3c3", "b2b2b2b2b2b2b2b2", "POST /payments/authorizations", "SPAN_KIND_SERVER", 4_000, 7_000, status={"code": "STATUS_CODE_ERROR"})]}],
        },
    ]}}


def test_parses_services_hierarchy_attributes_and_events():
    [trace] = traces.group_traces(traces.parse_spans(_document()))

    assert trace.trace_id == TRACE_ID
    assert trace.services == {"order-service", "payment-service"}
    transaction = trace.named("order.transaction")[0]
    assert trace.parent_of(transaction).name == "POST /orders"
    assert transaction.kind == "INTERNAL" and transaction.status == "ERROR"
    assert transaction.attributes == {"retry.count": 2, "sampling.debug": True}
    assert transaction.events[0].attributes["order.state.to"] == "InventoryReserved"
    assert [root.name for root in trace.roots()] == ["POST /orders"]


def test_string_and_numeric_enum_spellings_are_equivalent():
    [trace] = traces.group_traces(traces.parse_spans(_document()))

    payment = trace.by_service("payment-service", kind="SERVER")[0]
    assert payment.status == "ERROR"


def test_base64_ids_are_normalized_to_hex():
    encoded = base64.b64encode(bytes.fromhex(TRACE_ID)).decode()

    assert traces.normalize_id(encoded, 16) == TRACE_ID
    assert traces.normalize_id(TRACE_ID.upper(), 16) == TRACE_ID
    assert traces.normalize_id(base64.b64encode(bytes(8)).decode(), 8) == ""


def test_overlap_detects_concurrent_spans():
    [trace] = traces.group_traces(traces.parse_spans(_document()))
    server, transaction, payment = trace.spans

    assert transaction.overlaps(payment)
    assert not traces.Span(**{**payment.__dict__, "start_unix_nano": 8_000, "end_unix_nano": 8_500}).overlaps(
        traces.Span(**{**payment.__dict__, "start_unix_nano": 9_000, "end_unix_nano": 9_500}))


def test_traceresponse_header_yields_the_trace_id():
    assert trace_id_from_traceresponse(f"00-{TRACE_ID}-a1a1a1a1a1a1a1a1-01") == TRACE_ID
    assert trace_id_from_traceresponse(None) == ""
