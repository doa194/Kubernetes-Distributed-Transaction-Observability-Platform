"""Runs one scenario from start to finish.

Lifecycle: lock -> restore interrupted runs -> pre-flight -> close circuit breakers -> faults ->
traffic (with Kubernetes actions) -> restore -> identifiers -> application checks -> telemetry
checks -> record -> unlock.
Restoring always happens, also after failures and interruptions (Ctrl+C), and every change is
journaled before it is made so `scenarioctl reset` can finish the job after a crash.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import traceback

import httpx

from txplatform import console, identity, kube, portforward, runlock
from txplatform.scenarios import contracts, control, expectations, records
from txplatform.scenarios.journal import Journal, restore_plan
from txplatform.scenarios.schema import DOWNSTREAM_SERVICES, KubernetesAction, Scenario
from txplatform.scenarios.workload import run_steps

REQUIRED_DEPLOYMENTS = {
    kube.NS_APP: ["order-service", *DOWNSTREAM_SERVICES, "keycloak"],
    kube.NS_GATEWAY: ["kong-gateway", "kong-controller"],
    kube.NS_OBSERVABILITY: ["jaeger-collector", "jaeger-query", "prometheus-server"],
}
REQUIRED_STATEFULSETS = {kube.NS_OBSERVABILITY: ["opensearch-traces"]}


def preflight() -> list[str]:
    """Problems that make a run meaningless; an empty list means the platform is ready."""
    problems: list[str] = []
    for namespace, deployments in REQUIRED_DEPLOYMENTS.items():
        for deployment in deployments:
            try:
                if not kube.deployment_available(namespace, deployment):
                    problems.append(f"{namespace}/{deployment} is not available")
            except Exception as error:  # noqa: BLE001 - reported, not raised
                problems.append(f"{namespace}/{deployment}: {error}")
    for namespace, statefulsets in REQUIRED_STATEFULSETS.items():
        for statefulset in statefulsets:
            if not kube.statefulset_ready(namespace, statefulset):
                problems.append(f"{namespace}/{statefulset} is not ready")
    if problems:
        return problems
    try:
        with identity.https_client(identity.GATEWAY_URL) as gateway:
            status = gateway.get("/orders/00000000-0000-0000-0000-000000000000").status_code
        if status != 401:
            problems.append(f"the gateway route answered {status} instead of 401 for an unauthenticated request")
        identity.TokenProvider().token("scenario-runner")
    except Exception as error:  # noqa: BLE001
        problems.append(f"gateway or identity provider not usable: {error}")
    leftovers = control.leftover_faults()
    if leftovers:
        problems.append(f"fault rules left from an earlier run: {leftovers}; run `scenarioctl reset`")
    return problems


def restore(record: records.RunRecord) -> None:
    for step in restore_plan(Journal(record.journal_path).entries()):
        if step.kind == "clear-faults":
            control.clear_faults(step.target)
        elif step.kind == "scale":
            control.restore_replicas(step.target, step.replicas or 0)
        elif step.kind == "wait-available":
            control.wait_rollout_complete(step.target, timeout=600)
    record.restored = True


def restore_interrupted_runs() -> list[str]:
    """Undoes the journaled changes of runs whose process ended before restoring the platform."""
    actions: list[str] = []
    for record in records.unrestored_runs():
        restore(record)
        if record.status == "running":
            record.status = "interrupted"
        record.save()
        actions.append(f"restored interrupted run {record.run_id}")
    return actions


def reset(force: bool = False) -> list[str]:
    """Brings the platform back to a clean state: finishes restores of interrupted runs, removes every
    fault rule and closes circuit breakers."""
    actions: list[str] = []
    if force and runlock.release(force=True):
        actions.append("released the experiment lock")
    actions += restore_interrupted_runs()
    for service in DOWNSTREAM_SERVICES:
        removed = control.clear_faults(service)
        if removed:
            actions.append(f"removed {removed} fault rule(s) from {service}")
    control.reset_circuit_breakers()
    actions.append("closed all circuit breakers")
    return actions


async def _kubernetes_action(action: KubernetesAction, record: records.RunRecord, journal: Journal, started: float) -> None:
    await asyncio.sleep(max(0.0, started + action.at_seconds - time.monotonic()))
    event: dict = {"action": action.action, "workload": action.workload, "at": time.time()}
    if action.action == "delete-pod":
        event["pod"] = await asyncio.to_thread(control.delete_pod, action.workload, journal)
    elif action.action == "rollout-restart":
        # Remembered so the telemetry checks can tell replaced pods from new ones later.
        event["podsBefore"] = await asyncio.to_thread(control.ready_pods, action.workload)
        await asyncio.to_thread(control.rollout_restart, action.workload, journal)
    else:
        original = await asyncio.to_thread(control.scale, action.workload, action.replicas or 0, journal)
        event["originalReplicas"] = original
        if action.restore_after_seconds:
            await asyncio.sleep(action.restore_after_seconds)
            await asyncio.to_thread(control.restore_replicas, action.workload, original)
            event["restoredAt"] = time.time()
    record.kubernetes_events.append(event)


async def _traffic(scenario: Scenario, record: records.RunRecord, journal: Journal, tokens: identity.TokenProvider) -> None:
    started = time.monotonic()
    actions = [asyncio.create_task(_kubernetes_action(a, record, journal, started)) for a in scenario.kubernetes]
    result = await run_steps(scenario.workload, record.run_id, scenario.id, tokens)
    await asyncio.gather(*actions)
    record.workload_started_at, record.workload_finished_at = result.started_at, result.finished_at
    record.requests = [r.to_json() for r in result.requests]


def _read_back_orders(record: records.RunRecord, tokens: identity.TokenProvider) -> None:
    """Final order states, read directly from OrderService so the gateway's rate limit does not interfere."""
    order_ids = sorted({r["order_id"] for r in record.requests if r["order_id"]})
    if not order_ids:
        return
    headers = {"Authorization": f"Bearer {tokens.token('order-reader')}"}
    with portforward.forward(kube.NS_APP, "svc/order-service", 8080) as port:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, headers=headers) as client:
            for order_id in order_ids:
                response = client.get(f"/orders/{order_id}")
                if response.status_code == 200:
                    record.orders[order_id] = response.json()


def _application_checks(scenario: Scenario, record: records.RunRecord) -> None:
    expected = scenario.expect.application
    results = [expectations.evaluate_statuses(expected, [r["status"] for r in record.requests])]
    states = [order["state"] for order in record.orders.values()]
    results.append(expectations.evaluate_states(expected, states))
    failed = [order for order in record.orders.values() if order["state"].endswith("Failed")]
    results.append(expectations.evaluate_compensation(expected, failed))
    if expected.authorizations_per_order is not None:
        summaries = control.payment_authorizations(sorted(record.orders))
        results.append(expectations.evaluate_authorizations(expected, {o: s["authorizations"] for o, s in summaries.items()}))
    for result in results:
        if result is not None:
            record.add_check(result.name, result.passed, result.detail)


def run(scenario: Scenario, verify_telemetry: bool = True) -> records.RunRecord:
    record = records.RunRecord(records.new_run_id(scenario.id), scenario.id, started_at=dt.datetime.now(dt.UTC).isoformat())
    journal = Journal(record.journal_path)
    tokens = identity.TokenProvider()
    # The record is only written once the lock is held, so a refused run leaves nothing to restore.
    with runlock.held(f"scenario {scenario.id} ({record.run_id})"):
        # A previous run may have crashed in the middle of its changes; undo them before starting.
        for action in restore_interrupted_runs():
            console.info(action)
        record.save()
        try:
            problems = preflight()
            if problems:
                record.status = "error"
                record.error = "pre-flight failed: " + "; ".join(problems)
                record.restored = True
                return record
            # Circuit breakers opened by an earlier run must not affect this one.
            control.reset_circuit_breakers()
            if verify_telemetry:
                contracts.snapshot_metrics(scenario, record)
            for fault in scenario.faults:
                pods = control.apply_fault(fault, record.run_id, journal)
                record.faults.append({"service": fault.service, "operation": fault.operation, "mode": fault.mode, "pods": pods})
                console.info(f"fault {fault.mode} on {fault.operation} in {', '.join(pods)}")
            console.info(f"sending traffic ({len(scenario.workload)} step(s))")
            asyncio.run(_traffic(scenario, record, journal, tokens))
            restore(record)
            _read_back_orders(record, tokens)
            _application_checks(scenario, record)
            if verify_telemetry and scenario.expect.telemetry:
                console.info("verifying telemetry (waits for the tail-sampling decision)")
                contracts.verify(scenario, record)
            record.status = "passed" if record.passed else "failed"
        except KeyboardInterrupt:
            record.status = "interrupted"
            raise
        except Exception as error:  # noqa: BLE001 - recorded, then the platform is restored
            record.status = "error"
            record.error = f"{type(error).__name__}: {error}\n{traceback.format_exc(limit=5)}"
        finally:
            if not record.restored:
                restore(record)
            record.finished_at = dt.datetime.now(dt.UTC).isoformat()
            record.save()
    return record
