"""End-to-end tests: the most important journeys through the whole running platform.

Each test runs a scenario exactly as an operator would (traffic through Kong, application results,
stored traces and span metrics) and only asserts the overall verdict; the detailed expectations live
in scenarios/*.yaml and the trace checks are unit tested against recorded traces.

Run against a deployed platform with `uv run pytest -m e2e`.
"""

import pytest

from txplatform.scenarios import catalog, engine, records

pytestmark = pytest.mark.e2e


def _failures(record: records.RunRecord) -> list[str]:
    return [f"[{check['layer']}] {check['name']}: {check['detail']}" for check in record.checks if not check["passed"]]


@pytest.mark.parametrize("scenario_id", ["normal-order", "payment-timeout"])
def test_critical_scenario_passes_end_to_end(scenario_id):
    record = engine.run(catalog.get(scenario_id))

    assert record.status == "passed", _failures(record) or record.error


def test_verifier_rejects_telemetry_that_does_not_match_what_happened():
    # Negative control: ordinary successful orders are verified against the payment-retry contract.
    # The orders succeed, but their traces show no retry and no idempotent replay.
    normal = catalog.get("normal-order")
    retry_contract = catalog.get("payment-retry").expect.telemetry
    mismatched = normal.model_copy(update={"expect": normal.expect.model_copy(update={"telemetry": retry_contract})})

    record = engine.run(mismatched)

    failed = {check["name"] for check in record.checks if not check["passed"]}
    application = [check for check in record.checks if check["layer"] == "application"]
    assert application and all(check["passed"] for check in application)
    assert record.status == "failed"
    assert {"retry attempts", "idempotent replay"} <= failed
