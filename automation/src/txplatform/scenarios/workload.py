"""Traffic generation for scenarios.

Requests are sent on a fixed schedule ("open loop"): each request starts at its planned time even if
earlier ones are still running. A slowing system therefore shows up as higher latency instead of
silently lowering the load, which is what makes latency and throughput measurements honest.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass, field

import httpx

from txplatform import identity, orders
from txplatform.scenarios.schema import Profile, WorkloadStep

TEMPLATES: dict[str, dict] = {
    "normal": orders.NORMAL_ORDER,
    # 6 x 950.00 exceeds FraudService's 5000.00 limit.
    "high-risk": {"items": [{"sku": "SKU-2001", "quantity": 6}], "paymentToken": "tok_test_approved", "shippingZone": "domestic"},
    "declined": {"items": [{"sku": "SKU-1001", "quantity": 1}], "paymentToken": "tok_test_declined", "shippingZone": "domestic"},
    "restricted-zone": {"items": [{"sku": "SKU-1001", "quantity": 1}], "paymentToken": "tok_test_approved", "shippingZone": "restricted"},
    "out-of-stock": {"items": [{"sku": "SKU-9000", "quantity": 1}], "paymentToken": "tok_test_approved", "shippingZone": "domestic"},
    # About 32 KB, twice Kong's request size limit.
    "oversized": {**orders.NORMAL_ORDER, "padding": "x" * 32_000},
    "malformed": {"items": "not-a-list", "paymentToken": 42},
}
_ORDER_ID = re.compile(r"/orders/([0-9a-fA-F-]{36})")


def schedule(step: WorkloadStep) -> list[float]:
    """Planned start offsets in seconds for every request of a step (pure, deterministic)."""
    if step.profile in {Profile.SINGLE}:
        return [0.0]
    if step.profile in {Profile.BURST, Profile.CONCURRENT}:
        return [0.0] * step.requests
    rate, duration = step.rate_per_second or 0.0, step.duration_seconds or 0.0
    if step.profile == Profile.STEADY:
        return [index / rate for index in range(int(rate * duration))]
    # Spike: baseline rate, then the spike rate in the middle of the step, then baseline again.
    spike_seconds = step.spike_seconds or 0.0
    spike_start = (duration - spike_seconds) / 2
    offsets: list[float] = []
    offsets += [i / rate for i in range(int(rate * spike_start))]
    spike_rate = step.spike_rate_per_second or rate
    offsets += [spike_start + i / spike_rate for i in range(int(spike_rate * spike_seconds))]
    tail_start = spike_start + spike_seconds
    offsets += [tail_start + i / rate for i in range(int(rate * (duration - tail_start)))]
    return offsets


def tamper(token: str) -> str:
    """Changes the signature's last character so the token is well-formed but its signature is invalid."""
    head, _, signature = token.rpartition(".")
    replacement = "A" if signature[-1] != "A" else "B"
    return f"{head}.{signature[:-1]}{replacement}"


@dataclass
class RequestRecord:
    step: int
    sequence: int
    label: str
    planned_offset: float
    started_at: float
    duration_ms: float
    status: int
    correlation_id: str
    kong_request_id: str
    trace_id: str
    order_id: str
    state: str
    error: str = ""

    def to_json(self) -> dict:
        return self.__dict__.copy()


@dataclass
class WorkloadResult:
    requests: list[RequestRecord] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0


def _order_reference(response: httpx.Response) -> tuple[str, str]:
    body: dict = {}
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        pass
    order_id = str(body.get("orderId", "")) if isinstance(body, dict) else ""
    if not order_id:
        match = _ORDER_ID.search(response.headers.get("location", ""))
        order_id = match.group(1) if match else ""
    state = str(body.get("state", "")) if isinstance(body, dict) else ""
    return order_id, state


async def _send(
    client: httpx.AsyncClient,
    tokens: identity.TokenProvider,
    step_index: int,
    step: WorkloadStep,
    sequence: int,
    offset: float,
    started: float,
    run_id: str,
    scenario_id: str,
    limiter: asyncio.Semaphore,
) -> RequestRecord:
    await asyncio.sleep(max(0.0, started + offset - time.monotonic()))
    correlation_id = f"{run_id}-{step_index}-{sequence:05d}"
    headers = {
        "X-Correlation-ID": correlation_id, "X-Scenario-Id": scenario_id, "X-Scenario-Run-Id": run_id,
        # A fake secret the telemetry checks search for: it must never appear in any span.
        "X-Api-Key": f"canary-{run_id}",
    }
    if step.identity in {"scenario-runner", "order-reader"}:
        headers["Authorization"] = f"Bearer {await asyncio.to_thread(tokens.token, step.identity)}"
    elif step.identity == "tampered":
        headers["Authorization"] = f"Bearer {tamper(await asyncio.to_thread(tokens.token, 'scenario-runner'))}"
    if step.debug:
        headers["X-Debug-Trace"] = "true"
    body = copy.deepcopy(TEMPLATES[step.order])
    async with limiter:
        began = time.monotonic()
        try:
            response = await client.post("/orders", json=body, headers=headers)
        except httpx.HTTPError as error:
            return RequestRecord(step_index, sequence, step.label, offset, began, (time.monotonic() - began) * 1000, 0, correlation_id, "", "", "", "", f"{type(error).__name__}: {error}")
    order_id, state = _order_reference(response)
    return RequestRecord(
        step_index, sequence, step.label, offset, began, (time.monotonic() - began) * 1000, response.status_code,
        response.headers.get("x-correlation-id", ""), response.headers.get("x-kong-request-id", ""),
        orders.trace_id_of(response), order_id, state,
    )


async def run_steps(steps: list[WorkloadStep], run_id: str, scenario_id: str, tokens: identity.TokenProvider) -> WorkloadResult:
    result = WorkloadResult(started_at=time.time())
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    async with httpx.AsyncClient(base_url=identity.GATEWAY_URL, verify=identity.ca_context(), timeout=30, limits=limits) as client:
        for index, step in enumerate(steps):
            if step.delay_seconds:
                await asyncio.sleep(step.delay_seconds)
            limiter = asyncio.Semaphore(step.concurrency if step.profile == Profile.CONCURRENT else 10_000)
            started = time.monotonic()
            records = await asyncio.gather(*(
                _send(client, tokens, index, step, sequence, offset, started, run_id, scenario_id, limiter)
                for sequence, offset in enumerate(schedule(step))
            ))
            result.requests.extend(records)
    result.finished_at = time.time()
    return result
