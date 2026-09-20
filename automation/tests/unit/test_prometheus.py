"""Prometheus helpers must match Collector metric names across naming versions and fail loudly."""

import pytest

from txplatform.prometheus import metric_selector, parse_vector


def test_selector_matches_names_with_and_without_total_suffix():
    assert metric_selector("otelcol_receiver_accepted_spans") == '{__name__=~"otelcol_receiver_accepted_spans(_total)?"}'
    assert metric_selector("otelcol_receiver_accepted_spans_total") == '{__name__=~"otelcol_receiver_accepted_spans(_total)?"}'


def test_selector_adds_exact_label_matchers():
    assert metric_selector("traces_span_metrics_calls", service_name="order-service") == (
        '{__name__=~"traces_span_metrics_calls(_total)?",service_name="order-service"}'
    )


def test_vector_values_are_parsed_as_floats():
    payload = {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"job": "jaeger-collector"}, "value": [1758103200.0, "42"]},
    ]}}

    [sample] = parse_vector(payload)

    assert sample.labels == {"job": "jaeger-collector"} and sample.value == 42.0


def test_failed_query_raises():
    with pytest.raises(RuntimeError, match="parse error"):
        parse_vector({"status": "error", "error": "parse error"})
