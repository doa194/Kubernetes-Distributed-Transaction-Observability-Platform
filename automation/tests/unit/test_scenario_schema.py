"""Scenario files must be rejected when they describe an inconsistent experiment, before anything runs."""

import pytest

from txplatform.scenarios import catalog
from txplatform.scenarios.schema import FaultSpec, KubernetesAction, WorkloadStep

MINIMAL = """
schemaVersion: 1
id: sample-scenario
title: Sample
description: Sample scenario
category: workload
workload:
  - profile: single
expect:
  application:
    statuses: {201: {all: true}}
"""


def _fault(**overrides) -> dict:
    return {"service": "payment-service", "operation": "payment.authorize", "mode": "latency", "delayMs": 100, "ttlSeconds": 60, **overrides}


def test_every_scenario_in_the_catalog_is_valid_and_named_after_its_id():
    scenarios = catalog.load_all()

    assert len(scenarios) >= 18
    assert all(scenario.expect.telemetry is not None for scenario in scenarios.values())


def test_unknown_fields_are_rejected_so_typos_cannot_change_the_experiment():
    with pytest.raises(catalog.CatalogError, match="delayMsec"):
        catalog.parse(MINIMAL.replace("  - profile: single", "  - profile: single\n    delayMsec: 5"))


def test_a_fault_without_expiry_is_rejected():
    fault = _fault()
    del fault["ttlSeconds"]

    with pytest.raises(ValueError, match="ttlSeconds"):
        FaultSpec.model_validate(fault)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"operation": "shipping.create"}, "has no operation"),
        ({"delayMs": None}, "latency faults need delayMs"),
        ({"mode": "http-error", "delayMs": None}, "need statusCode"),
        ({"mode": "intermittent", "statusCode": 503, "failAttempts": 1, "everyNth": 2}, "exactly one of"),
        ({"mode": "http-error", "statusCode": 500, "phase": "after-effect"}, "only latency faults"),
        ({"operation": "readiness", "mode": "readiness-loss"}, "must set scoped: false"),
        ({"mode": "timeout", "delayMs": None, "operation": "readiness"}, "readiness-loss faults must use"),
    ],
)
def test_inconsistent_fault_parameters_are_rejected(overrides, message):
    with pytest.raises(ValueError, match=message):
        FaultSpec.model_validate(_fault(**overrides))


def test_scoped_faults_carry_the_run_id_and_unscoped_faults_do_not():
    scoped = FaultSpec.model_validate(_fault())
    unscoped = FaultSpec.model_validate(_fault(operation="readiness", mode="readiness-loss", delayMs=None, scoped=False))

    assert scoped.to_rule("run-1")["scenarioRunId"] == "run-1"
    assert "scenarioRunId" not in unscoped.to_rule("run-1")
    assert "statusCode" not in scoped.to_rule("run-1")


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"profile": "steady", "ratePerSecond": 5}, "needs ratePerSecond and durationSeconds"),
        ({"profile": "spike", "ratePerSecond": 5, "durationSeconds": 10, "spikeRatePerSecond": 50, "spikeSeconds": 10}, "shorter than"),
        ({"profile": "single", "identity": "order-reader", "debug": True}, "traces.debug permission"),
    ],
)
def test_inconsistent_workload_steps_are_rejected(step, message):
    with pytest.raises(ValueError, match=message):
        WorkloadStep.model_validate(step)


@pytest.mark.parametrize(
    ("action", "message"),
    [
        ({"action": "scale", "workload": "fraud-service"}, "replicas is required"),
        ({"action": "delete-pod", "workload": "fraud-service", "replicas": 1}, "only allowed for scale"),
        ({"action": "delete-pod", "workload": "keycloak"}, "workload must be one of"),
    ],
)
def test_inconsistent_kubernetes_actions_are_rejected(action, message):
    with pytest.raises(ValueError, match=message):
        KubernetesAction.model_validate(action)
