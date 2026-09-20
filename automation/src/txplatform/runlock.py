"""A cluster-wide lock that lets only one experiment generate traffic at a time.

Scenario runs and traffic-generating validation suites compare exact counts (for example span
metrics before and after), which only works when nothing else sends requests meanwhile. The lock is
a Kubernetes Lease: every operator sees who holds it, and a lock left behind by a crashed process
expires on its own because its holder stops renewing it.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from txplatform import kube

LEASE_NAME = "txplatform-experiment-lock"
LEASE_NAMESPACE = kube.NS_APP
LEASE_DURATION_SECONDS = 90
RENEW_INTERVAL_SECONDS = 20


class LockHeldError(RuntimeError):
    """Another experiment holds the lock."""


@dataclass(frozen=True)
class LeaseState:
    holder: str
    purpose: str
    renewed_at: dt.datetime
    duration_seconds: int


def is_expired(state: LeaseState, now: dt.datetime) -> bool:
    """A lease whose holder stopped renewing it for longer than its duration may be taken over."""
    return now - state.renewed_at > dt.timedelta(seconds=state.duration_seconds)


def _holder_identity() -> str:
    return f"{socket.gethostname()}/{os.getpid()}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _read() -> tuple[client.V1Lease | None, LeaseState | None]:
    try:
        lease = kube.clients().coordination.read_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE)
    except ApiException as error:
        if error.status == 404:
            return None, None
        raise
    renewed = lease.spec.renew_time or lease.spec.acquire_time or _now()
    purpose = (lease.metadata.annotations or {}).get("txplatform.io/purpose", "")
    return lease, LeaseState(lease.spec.holder_identity or "", purpose, renewed, lease.spec.lease_duration_seconds or LEASE_DURATION_SECONDS)


def current() -> LeaseState | None:
    return _read()[1]


def _body(purpose: str, resource_version: str | None = None) -> client.V1Lease:
    now = _now()
    return client.V1Lease(
        metadata=client.V1ObjectMeta(
            name=LEASE_NAME, namespace=LEASE_NAMESPACE, resource_version=resource_version,
            labels={"app.kubernetes.io/part-of": "txplatform"},
            annotations={"txplatform.io/purpose": purpose},
        ),
        spec=client.V1LeaseSpec(holder_identity=_holder_identity(), lease_duration_seconds=LEASE_DURATION_SECONDS, acquire_time=now, renew_time=now),
    )


def acquire(purpose: str) -> None:
    coordination = kube.clients().coordination
    lease, state = _read()
    if lease is None:
        try:
            coordination.create_namespaced_lease(LEASE_NAMESPACE, _body(purpose))
            return
        except ApiException as error:
            if error.status != 409:
                raise
            lease, state = _read()
    assert state is not None and lease is not None
    if state.holder == _holder_identity():
        return
    if not is_expired(state, _now()):
        raise LockHeldError(f"'{state.purpose}' holds the experiment lock ({state.holder}, renewed {state.renewed_at:%H:%M:%S} UTC)")
    # Replacing with the read resourceVersion fails if someone else took over at the same moment.
    coordination.replace_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, _body(purpose, lease.metadata.resource_version))


def renew() -> None:
    lease, state = _read()
    if lease is None or state is None or state.holder != _holder_identity():
        return
    lease.spec.renew_time = _now()
    kube.clients().coordination.replace_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, lease)


def release(force: bool = False) -> bool:
    lease, state = _read()
    if lease is None or state is None:
        return False
    if not force and state.holder != _holder_identity():
        return False
    kube.clients().coordination.delete_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE)
    return True


@contextmanager
def held(purpose: str) -> Iterator[None]:
    """Holds the lock for the duration of the block and renews it in the background."""
    acquire(purpose)
    stop = threading.Event()

    def keep_renewing() -> None:
        while not stop.wait(RENEW_INTERVAL_SECONDS):
            try:
                renew()
            except Exception:  # noqa: BLE001 - a missed renewal is retried; the lease tolerates it
                pass

    renewer = threading.Thread(target=keep_renewing, daemon=True)
    renewer.start()
    try:
        yield
    finally:
        stop.set()
        release()
