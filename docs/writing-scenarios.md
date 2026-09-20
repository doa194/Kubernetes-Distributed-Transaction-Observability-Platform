# Writing scenarios

A scenario is a YAML file in `scenarios/` that describes an experiment: which faults to inject,
which cluster changes to make, which traffic to send, and what must be true afterwards. The runner
turns this description into actions and evidence. This guide walks through adding a scenario and
then documents every field.

---

## A complete example

This walkthrough adds a scenario that asks: *"When a card is declined, does the order fail cleanly —
stock released, nothing charged, and no error in the traces?"* (The catalog does not include it; it
is used here because it needs every part of a scenario and was verified against the running
platform while writing this guide.)

### Step 1 — Describe the experiment

Create `scenarios/declined-payment.yaml`. The file name must equal the `id`.

```yaml
# A declined card is a business outcome: the order fails cleanly and nothing stays reserved.
schemaVersion: 1
id: declined-payment
title: Declined payment
description: >
  PaymentService declines the card of three orders (test token tok_test_declined). The orders fail
  with 422, the reserved stock is released, no payment is recorded, and the traces are not marked
  as errors.
category: application-fault
workload:
  - profile: concurrent
    requests: 3
    concurrency: 3
    order: declined          # an order template that uses the declining test token
    debug: true              # keep every trace, so the checks do not depend on sampling
expect:
  application:
    statuses: { 422: { all: true } }
    states: { PaymentFailed: { all: true } }
    compensation: [ReleaseInventory]
    authorizationsPerOrder: 0
  telemetry:
    contract: declined-payment
```

No fault is needed: the real payment rule declines the test token. The expectations follow from the
platform's rules — a declined payment is a business rejection (422), and the only step to undo is the
stock reservation ([transaction-flow.md](transaction-flow.md)).

### Step 2 — Check that the file is valid

```bash
uv run scenarioctl show declined-payment
```

The file is validated strictly, so a typo fails loudly instead of silently changing the experiment.
Writing `request: 3` instead of `requests: 3`, for example, produces:

```text
[FAIL] CatalogError: declined-payment.yaml is invalid:
1 validation error for Scenario
workload.0.request
  Extra inputs are not permitted [type=extra_forbidden, input_value=3, input_type=int]
```

### Step 3 — Write the telemetry contract

The `contract` names a Python function that checks what the telemetry says about the run. Every
scenario in the catalog must have one (a unit test enforces it). Add the function to
`automation/src/txplatform/scenarios/contracts.py`, reusing the helper for failed orders:

```python
def declined_payment(ctx: Context) -> None:
    _failed_flow(ctx, 422, ["InventoryReserved", "PaymentFailed"],
                 {"kong-gateway", "order-service", "inventory-service", "fraud-service", "payment-service"}, {"shipping-service"},
                 ("payment.authorize", "compensation.inventory.release"), ("shipping.create", "compensation.payment.void"),
                 technical=False)
```

The arguments say: requests that ended with 422 must have stored traces; the state events must be
`InventoryReserved → PaymentFailed`; these services must appear and ShippingService must not; these
stages must and must not run; and the failure is **not** technical, so nothing may be marked as an
error. The helper also applies the common checks (integrity, Kubernetes metadata, correlation,
sensitive data) and the stage-order check.

Register it in the `CONTRACTS` dictionary at the end of the file:

```python
    "declined-payment": declined_payment,
```

### Step 4 — Run it

```bash
uv run scenarioctl run declined-payment
```

```text
==> Run 20260918T095116Z-declined-payment-bf41: PASSED
    requests: 3 {422: 3}; orders read back: 3
[ OK ] [application] HTTP statuses: 422x3
[ OK ] [application] order states: PaymentFailedx3
[ OK ] [application] compensation: 3 failed orders compensated with ['ReleaseInventory']
[ OK ] [application] payment authorizations: 3 orders with exactly 0 authorization(s)
[ OK ] [telemetry] trace integrity: 3 trace(s) as expected
[ OK ] [telemetry] kubernetes metadata: 3 trace(s) as expected
[ OK ] [telemetry] correlation: 3 trace(s) as expected
[ OK ] [telemetry] sensitive data: 3 trace(s) as expected
[ OK ] [telemetry] topology: 3 trace(s) as expected
[ OK ] [telemetry] stages: 3 trace(s) as expected
[ OK ] [telemetry] stage order: 3 trace(s) as expected
[ OK ] [telemetry] state events: 3 trace(s) as expected
[ OK ] [telemetry] error marking: 3 trace(s) as expected
```

Then make sure the result is stable and the catalog is still valid:

```bash
uv run scenarioctl run declined-payment --repeat 3
uv run pytest        # the unit tests validate every scenario file in the catalog
```

---

## Field reference

### Header

| Field | Rules |
| --- | --- |
| `schemaVersion` | Always `1` |
| `id` | 3–41 characters: a lowercase letter, then lowercase letters, digits and hyphens; equal to the file name |
| `title`, `description` | Shown by `scenarioctl list` and `show`; the description should state the expected outcome |
| `category` | `application-fault`, `kubernetes-fault`, `edge`, `security` or `workload` |

### `faults` (optional)

Each entry creates one fault rule in one or all pods of a dependency
([fault-injection.md](fault-injection.md)).

| Field | Rules |
| --- | --- |
| `service` | `inventory-service`, `fraud-service`, `payment-service` or `shipping-service` |
| `operation` | An operation of that service: `inventory.reserve`, `inventory.release`, `fraud.evaluate`, `payment.authorize`, `payment.void`, `shipping.create`, or `readiness` |
| `mode` | `latency`, `http-error`, `timeout`, `intermittent`, `business-rejection` or `readiness-loss` (only with `readiness`) |
| `ttlSeconds` | **Required**, 10–1 800. The rule expires even if nothing removes it |
| `delayMs` | 1–120 000; required for `latency` |
| `statusCode` | 400–599; required for `http-error` and `intermittent` |
| `failAttempts` / `everyNth` | Exactly one of them for `intermittent` |
| `phase` | `before-effect` (default) or `after-effect` (only for `latency`) |
| `maxActivations` | Stop after this many applications |
| `replicas` | `all` (default) or `one` — degrade a single replica |
| `scoped` | `true` (default): only this run's requests are affected. Must be `false` for `readiness-loss`, which affects the whole pod |

### `kubernetes` (optional)

Changes to the cluster, timed relative to the start of the traffic.

```yaml
kubernetes:
  - action: scale              # scale | delete-pod | rollout-restart
    workload: jaeger-collector
    replicas: 0                # required for scale, not allowed otherwise
    atSeconds: 15              # 0–600 seconds after the traffic started
    restoreAfterSeconds: 45    # optional, scale only; restoration also happens at the end of the run
```

Allowed workloads are the five services, `jaeger-collector` and `opensearch-traces`. Kong and
Keycloak are excluded on purpose: without them no request could be routed or authenticated, so the
experiment could not observe anything. `delete-pod` deletes the first ready pod by name.

### `workload` (required)

A list of steps that run one after another. Requests are sent on a fixed schedule — each starts at
its planned time even if earlier ones are still running — so a slowing system shows up as higher
latency instead of quietly lowering the load.

| Profile | Sends | Required fields |
| --- | --- | --- |
| `single` | One request | – |
| `concurrent` | `requests` requests, at most `concurrency` at a time | `requests`, `concurrency` |
| `burst` | `requests` requests, all at once | `requests` |
| `steady` | `ratePerSecond` for `durationSeconds` | `ratePerSecond`, `durationSeconds` |
| `spike` | `ratePerSecond`, with `spikeRatePerSecond` for `spikeSeconds` in the middle | all four |

| Field | Rules |
| --- | --- |
| `order` | `normal`, `high-risk` (above the fraud limit), `declined`, `restricted-zone`, `out-of-stock`, `oversized` (about 32 KB), `malformed` |
| `identity` | `scenario-runner` (default), `order-reader` (read-only), `none` (no token), `tampered` (a valid token with a broken signature) |
| `debug` | Keep every trace of the step; requires the `scenario-runner` identity |
| `delaySeconds` | Wait before the step starts, for example until a Kubernetes action has taken effect |
| `label` | A name that the telemetry contract can use to select the step's requests |

Every request automatically carries a correlation id derived from the run id, the scenario and run
ids, and a canary header that the telemetry checks search for ([telemetry-contracts.md](telemetry-contracts.md)).

### `expect.application` (required)

| Field | Meaning |
| --- | --- |
| `statuses` | HTTP status → count rule. **Any status not listed must not occur** |
| `states` | Final order state → count rule (orders are read back after the run) |
| `compensation` | The steps every failed order must have completed, in this order |
| `authorizationsPerOrder` | The exact number of payment authorizations per created order, read from the payment ledger |

A count rule is `{ all: true }`, `{ exactly: n }`, `{ min: n }`, `{ max: n }` or `min` and `max`
together.

### `expect.telemetry`

| Field | Meaning |
| --- | --- |
| `contract` | The name of a function registered in `CONTRACTS` in `contracts.py` |
| `parameters` | Values passed to the contract, for example thresholds (`minShare: 0.6`) |

A contract receives a context object and records checks with `ctx.check(name, problems, subject)`;
an empty list of problems means the check passed. Building blocks:

| Helper | Use |
| --- | --- |
| `tracechecks.py` | Pure trace checks: integrity, topology, stages, stage order, state path, error marking, retries, idempotent replay, latency dominance, Kubernetes metadata, correlation, sensitive data |
| `_failed_flow(...)` | Everything a failed-order scenario needs, as in the example |
| `ctx.metric_delta(key)` | Counter change since the run started. Add the keys to `METRIC_KEYS` so the runner takes a snapshot before the traffic |
| `ctx.note(name, subject)` | A finding that does not fail the run but stays visible in its report |

## Guidelines

- **Ask one question.** A good title can be answered with yes or no.
- **Expect exact numbers** wherever the platform is deterministic, and explain in a comment when it
  is not (for example the rate-limit split in `kong-rate-limit`). A rule that cannot fail proves
  nothing.
- **Make the scenario prove its own premise.** `collector-outage` fails if the traces from the outage
  window exist — that would mean the outage never happened. `trace-storage-outage` fails if the
  Collector's queue stayed empty — that would mean the writes were never blocked.
- **Keep traffic small** unless the question is about load; three to five requests answer most
  questions and keep the circuit breakers (minimum 10 calls) closed.
- **Use the debug flag** when the checks need every trace; leave it off only when sampling is what
  you want to observe (as in `slow-payment`).
- **Keep fault TTLs short** — a few minutes — so an interrupted run heals itself
  ([experiment-safety.md](experiment-safety.md)).

## Related documents

- [Scenario catalog](scenario-catalog.md) — the existing scenarios, as further examples
- [Telemetry contracts](telemetry-contracts.md) — what the contract checks mean
- [Fault injection](fault-injection.md) — the fault modes in detail
- [Codebase guide](codebase-guide.md) — where the runner's code lives
