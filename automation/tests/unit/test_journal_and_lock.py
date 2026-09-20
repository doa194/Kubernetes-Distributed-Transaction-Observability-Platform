"""Crash recovery: the restore plan must undo every journaled change safely, and abandoned locks must expire."""

import datetime as dt

from txplatform import runlock
from txplatform.scenarios.journal import Journal, RestoreStep, restore_plan


def test_journal_entries_survive_on_disk_in_order(tmp_path):
    journal = Journal(tmp_path / "run" / "journal.jsonl")

    journal.record("fault", service="payment-service", pod="payment-1", operation="payment.authorize", mode="timeout")
    journal.record("scale", workload="shipping-service", originalReplicas=1, replicas=0)

    assert [entry["kind"] for entry in Journal(journal.path).entries()] == ["fault", "scale"]


def test_missing_journal_means_nothing_to_restore(tmp_path):
    assert restore_plan(Journal(tmp_path / "absent.jsonl").entries()) == []


def test_restore_removes_faults_then_restores_replicas_then_waits_for_availability():
    entries = [
        {"kind": "scale", "workload": "shipping-service", "originalReplicas": 1, "replicas": 0},
        {"kind": "fault", "service": "fraud-service", "pod": "fraud-1"},
        {"kind": "delete-pod", "workload": "payment-service", "pod": "payment-1", "uid": "u1"},
    ]

    assert restore_plan(entries) == [
        RestoreStep("clear-faults", "fraud-service"),
        RestoreStep("scale", "shipping-service", 1),
        RestoreStep("wait-available", "shipping-service"),
        RestoreStep("wait-available", "fraud-service"),
        RestoreStep("wait-available", "payment-service"),
    ]


def test_repeated_scaling_restores_the_original_count_not_an_intermediate_one():
    entries = [
        {"kind": "scale", "workload": "fraud-service", "originalReplicas": 2, "replicas": 1},
        {"kind": "scale", "workload": "fraud-service", "originalReplicas": 1, "replicas": 0},
    ]

    assert RestoreStep("scale", "fraud-service", 2) in restore_plan(entries)


def test_duplicate_entries_produce_each_restore_step_once():
    entries = [{"kind": "fault", "service": "fraud-service", "pod": pod} for pod in ("fraud-1", "fraud-2")]
    entries += [{"kind": "rollout-restart", "workload": "fraud-service"}] * 2

    assert restore_plan(entries) == [RestoreStep("clear-faults", "fraud-service"), RestoreStep("wait-available", "fraud-service")]


def test_lock_expires_only_after_its_holder_stopped_renewing_for_the_full_duration():
    renewed = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
    state = runlock.LeaseState("host/42", "scenario normal-order", renewed, duration_seconds=90)

    assert not runlock.is_expired(state, renewed + dt.timedelta(seconds=90))
    assert runlock.is_expired(state, renewed + dt.timedelta(seconds=91))
