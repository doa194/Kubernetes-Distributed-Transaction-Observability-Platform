"""Sampling tolerance bands and synthetic OTLP payloads used by the telemetry-pipeline suite."""

import pytest

from txplatform import otlp, stats


def test_band_is_centered_on_the_expected_share():
    band = stats.binomial_band(400, 0.25)

    assert band.expected == 100
    assert band.low < 100 < band.high
    assert band.contains(100) and not band.contains(20) and not band.contains(200)


def test_band_stays_within_possible_counts():
    band = stats.binomial_band(10, 0.95)

    assert 0 <= band.low and band.high == 10


@pytest.mark.parametrize(("trials", "probability"), [(0, 0.5), (10, 1.5)])
def test_invalid_band_inputs_are_rejected(trials, probability):
    with pytest.raises(ValueError):
        stats.binomial_band(trials, probability)


def test_payload_groups_spans_by_service_with_hex_ids_and_typed_attributes():
    root = otlp.SyntheticSpan("GET /health/live", error=True, attributes={"sampling.debug": True, "http.status_code": 429, "url.path": "/x"})
    kong = otlp.SyntheticSpan("kong", service="kong-gateway", trace_id=root.trace_id, parent_span_id=root.span_id)

    payload = otlp.build_payload([root, kong])

    services = [r["resource"]["attributes"][0]["value"]["stringValue"] for r in payload["resourceSpans"]]
    assert services == ["pipeline-probe", "kong-gateway"]
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
    assert span["status"]["code"] == otlp.STATUS_ERROR
    assert {"key": "sampling.debug", "value": {"boolValue": True}} in span["attributes"]
    assert {"key": "http.status_code", "value": {"intValue": "429"}} in span["attributes"]
    assert int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]) == 20_000_000
