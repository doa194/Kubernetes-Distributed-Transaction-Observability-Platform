# Scenario catalog

The platform ships eighteen experiments. Each one asks a precise question about the system — *"Is a
payment charged twice when its reply is lost?"* — and answers it with evidence: the application's
responses, the final order states, the payment ledger, the stored traces and the span metrics.

This document lists every scenario, what it does and what it proves, with the results of a complete
run on the local cluster.

---

## Running scenarios

```bash
uv run scenarioctl list                     # the eighteen scenarios
uv run scenarioctl show payment-retry       # the definition of one scenario
uv run scenarioctl run payment-retry        # run it and verify the results
uv run scenarioctl run payment-retry --repeat 3
```

To run the whole catalog (about 23 minutes):

```bash
for s in $(uv run scenarioctl list | cut -d' ' -f1); do uv run scenarioctl run "$s"; done
```

Each run follows the same lifecycle — take the experiment lock, inject faults, send traffic, restore
the platform, check the results, record everything under `.runs/` — described in
[experiment-safety.md](experiment-safety.md). Most scenarios take about 45 seconds, most of which is
spent waiting for the Collector's 20-second sampling decision before the traces can be read back.

## At a glance

Results of one complete run (all eighteen passed in 22 minutes):

| Scenario | Question | Measured result |
| --- | --- | --- |
| [`normal-order`](#normal-order) | Does the happy path produce a complete, correctly ordered trace? | 5 × 201, one authorization each |
| [`slow-payment`](#slow-payment) | Can a trace prove which dependency made an order slow? | 5 × 201; payment dominated every trace |
| [`payment-retry`](#payment-retry) | Is a payment charged twice when its reply is lost? | 4 × 201, exactly one authorization each |
| [`payment-timeout`](#payment-timeout) | What happens when a dependency never answers? | 3 × 504 after three attempts; compensated; nothing charged |
| [`inventory-failure`](#inventory-failure) | Is a server error handled without retrying it? | 3 × 502; stock released; payment never called |
| [`fraud-rejection`](#fraud-rejection) | Is a business rejection kept apart from technical errors? | 3 × 422; no error marking |
| [`shipping-failure`](#shipping-failure) | Are completed steps undone in reverse order? | 3 × 502; payment voided, then stock released |
| [`dependency-unavailable`](#dependency-unavailable) | How does a dependency with no pods look? | 3 × 504; no ShippingService spans |
| [`replica-degradation`](#replica-degradation) | Can traces blame one slow replica instead of a whole service? | 25 × 201; slow spans only from the degraded pod |
| [`pod-deletion`](#pod-deletion) | Can traces tell which pod served a request across a pod replacement? | 80 × 201; both pods named in traces |
| [`rolling-restart`](#rolling-restart) | Is a rolling restart invisible to customers? | 240 × 201; no error spans |
| [`readiness-loss`](#readiness-loss) | Does an unready pod stop receiving traffic? | 360 × 201; the unready pod stopped receiving requests |
| [`kong-rate-limit`](#kong-rate-limit) | Does the gateway shed excess load — and is every rejection counted? | 44 × 201, 76 × 429; every 429 counted |
| [`oversized-request`](#oversized-request) | Are oversized bodies stopped at the edge? | 5 × 413; OrderService never called |
| [`auth-denied`](#auth-denied) | Are bad credentials refused before any work happens? | 10 × 401, 5 × 403; no dependency called |
| [`traffic-spike`](#traffic-spike) | Do the application and the telemetry pipeline keep up with a spike? | 570 × 201, 180 × 429; no errors, no telemetry loss |
| [`collector-outage`](#collector-outage) | Does the business survive a telemetry outage? | 40 × 201; the tracing gap is asserted |
| [`trace-storage-outage`](#trace-storage-outage) | Is trace data lost when storage fails? | 15 × 201; all 15 traces delivered late; queue peak 12 of 200 |

## What every trace check includes

Scenarios that verify individual traces always apply the same baseline rules, in addition to their
own ([telemetry-contracts.md](telemetry-contracts.md)):

| Rule | Meaning |
| --- | --- |
| Trace integrity | Exactly one root, which is Kong's span; no span points to a missing parent (apart from the recognised Kong defect on 5xx answers, which is reported as a note) |
| Kubernetes metadata | Every .NET span names its namespace, pod, node and deployment |
| Correlation | Correlation id, run id, Kong request id and order id on the spans match the request that was sent |
| Sensitive data | No credential-like attribute name and no trace of the canary value sent with every request |

---

## Application faults

These scenarios change how one dependency behaves and check how OrderService reacts.

### `normal-order`

*The baseline: everything works.*

- **Setup:** no faults. Five orders, one after another, with the debug flag so every trace is kept.
- **Expected:** 5 × 201, all `Completed`, exactly one payment authorization per order.
- **Telemetry proof:** all six services in the trace; inventory and fraud overlap in time, payment
  starts after both, shipping after payment; the four state events in order; no error marking.

### `slow-payment`

*A slow but healthy dependency.*

- **Setup:** PaymentService waits 1.2 s before authorizing — below the 1.5 s attempt timeout, so no
  retry happens. Five simultaneous orders, **without** the debug flag.
- **Expected:** 5 × 201, one authorization each.
- **Telemetry proof:** the traces are stored by the Collector's *slow* policy alone; in every trace
  `payment.authorize` is the longest step and takes at least 60% of the transaction.

### `payment-retry`

*A lost reply must not charge the customer twice.*

- **Setup:** PaymentService records the authorization, then delays its **reply** by 2 s
  (`after-effect`), longer than the 1.5 s attempt timeout. Four orders, two at a time — few enough
  that the payment circuit breaker (minimum 10 calls) cannot open.
- **Expected:** 4 × 201, exactly **one** authorization per order in the payment ledger.
- **Telemetry proof:** exactly two attempts, numbered 1 and 2, with one `retry` event; the
  authorization is recorded once (`payment.authorization_recorded`); the second attempt is answered
  as an idempotent replay; the payment step accounts for at least half of a trace lasting 1.5 s or
  more — the time lost waiting for the first reply.
- **Why it matters:** this is the classic double-charge bug of retrying systems. The idempotency key
  `order-{orderId}-authorize` prevents it ([resilience.md](resilience.md)).

### `payment-timeout`

*A dependency that never answers.*

- **Setup:** PaymentService holds every authorization until the caller gives up. Three simultaneous
  orders — nine attempts, below the breaker's minimum of ten.
- **Expected:** 3 × 504, state `PaymentFailed`, compensation `VoidPayment` and `ReleaseInventory`,
  zero authorizations.
- **Telemetry proof:** three attempts numbered 1–3 with two `retry` events; shipping never runs;
  compensation starts only after the failed step; the transaction is marked as an error.

### `inventory-failure`

*A server error that is not retried.*

- **Setup:** InventoryService answers every reservation with 500. A 500 means "the server failed in
  an unknown way", which OrderService does not retry.
- **Expected:** 3 × 502, state `InventoryFailed`, compensation `ReleaseInventory`, zero
  authorizations.
- **Telemetry proof:** the fraud check still ran in parallel; payment and shipping never ran; the
  failure is marked as an error.

### `fraud-rejection`

*A business rejection is not an error.*

- **Setup:** no fault — the orders exceed FraudService's real amount limit (EUR 5 000).
- **Expected:** 3 × 422, state `FraudFailed`, compensation `ReleaseInventory`, zero authorizations.
- **Telemetry proof:** the transaction span is **not** marked as an error, although the order failed.
  Red must mean "something is broken", not "a customer was refused".

### `shipping-failure`

*The last step fails; everything before it is undone.*

- **Setup:** ShippingService answers 500 after the payment was authorized.
- **Expected:** 3 × 502, state `ShippingFailed`, compensation `VoidPayment` then `ReleaseInventory`,
  one (voided) authorization per order.
- **Telemetry proof:** the compensation spans start after the failed shipping step; the error marking
  and the state path end in `ShippingFailed`.

## Kubernetes faults

These scenarios change the cluster itself and check that the traces reflect what Kubernetes did.

### `dependency-unavailable`

*A dependency with no running pod.*

- **Setup:** ShippingService is scaled to zero; traffic starts 12 s later, when its endpoints are gone.
- **Expected:** 3 × 504 (not 503), state `ShippingFailed`, payment voided and stock released.
- **Telemetry proof:** three attempts at shipping, the step marked with failure kind `timeout`, and no
  ShippingService span at all.
- **Why 504:** the namespace's default-deny policy drops connection attempts to a Service without
  endpoints instead of refusing them, so each attempt waits for its 1-second timeout
  ([network-policies.md](network-policies.md#a-behavior-to-know-no-endpoints-means-a-timeout-not-a-refusal)).

### `replica-degradation`

*Only one of two replicas is slow.*

- **Setup:** one of the two FraudService pods adds 400 ms to every evaluation (below the 700 ms
  attempt timeout). The traffic is one burst of 25 simultaneous orders, just under the gateway's
  limit. With every request in flight at once, OrderService opens one connection per request and
  both replicas receive traffic; a slow steady rate would reuse one pooled connection that might
  never reach the degraded pod.
- **Expected:** 25 × 201.
- **Telemetry proof:** every Fraud span slower than 350 ms comes from the degraded pod, and the
  healthy pod's spans stay fast — latency attributed to a replica, not to a service.

### `pod-deletion`

*The only PaymentService pod is replaced under load.*

- **Setup:** 2 orders per second for 40 s; the PaymentService pod is deleted after 10 s.
- **Expected:** at least 50 × 201. A single replica cannot guarantee zero failures during its own
  replacement, so a bounded number of 503 and 504 answers is tolerated.
- **Measured:** 80 × 201 — the 5-second pre-stop delay kept the old pod serving until the
  replacement was ready.
- **Telemetry proof:** the traces name both the deleted pod and its replacement, and any failed
  request is traced too.

### `rolling-restart`

*A rollout must be invisible to customers.*

- **Setup:** FraudService (two replicas) is restarted with a rolling update 5 s into a 60-second
  workload at 4 orders per second.
- **Expected:** every order completes.
- **Measured:** 240 × 201.
- **Telemetry proof:** no OrderService error span during the rollout; the first requests were served
  by pods that were replaced, the last ones by the new pods.

### `readiness-loss`

*An unready pod must stop receiving traffic.*

- **Setup:** one of the two FraudService pods starts failing its readiness probe while it keeps
  serving; 4 orders per second for 90 s.
- **Expected:** every order completes.
- **Measured:** 360 × 201.
- **Telemetry proof:** no errors while the pod was unready; after at most 50 seconds (probe detection
  plus OrderService's 30-second pooled-connection lifetime) no request reached the unready pod.

## Edge and security

These scenarios test the protections in front of the application. Their requests mostly never reach
a service, so the evidence comes from span metrics rather than traces.

### `kong-rate-limit`

*The gateway sheds excess load.*

- **Setup:** 120 simultaneous orders against a limit of 30 per second.
- **Expected:** between 30 and 90 orders pass. The exact split depends on how the burst falls across
  one-second windows — three runs measured 77/43, 40/80 and 44/76.
- **Telemetry proof:** the metrics count exactly as many 429s at Kong as the client received, and
  OrderService counted only the requests that passed.

### `oversized-request`

*Oversized bodies are stopped at the edge.*

- **Setup:** five orders with a 32 KB body against the 16 KB limit.
- **Expected:** 5 × 413.
- **Telemetry proof:** all five counted at Kong; none reached OrderService.

### `auth-denied`

*Bad credentials are refused before any work happens.*

- **Setup:** five requests without a token, five with a tampered signature, five with a valid
  read-only token (`order-reader`, which lacks `orders.write`).
- **Expected:** exactly 10 × 401 and 5 × 403.
- **Telemetry proof:** the metrics count exactly those answers at OrderService, and InventoryService
  received no call at all.

## Load and telemetry outages

### `traffic-spike`

*A short burst of traffic.*

- **Setup:** 60 seconds at 5 orders per second, with a 10-second spike to 50 per second.
- **Expected:** at least 450 × 201 and between 100 and 300 × 429.
- **Measured:** 570 × 201, 180 × 429.
- **Telemetry proof:** every 429 counted; no OrderService error; the Collector neither refused spans
  nor dropped traces before their sampling decision; Jaeger's Monitor API shows the spike.

### `collector-outage`

*Telemetry fails; the business must not.*

- **Setup:** three phases of 1 order per second — before, during and after the Jaeger Collector is
  scaled to zero for 45 seconds.
- **Expected:** every order completes.
- **Measured:** 40 × 201.
- **Telemetry proof:** traces from before and after the outage are complete; the traces of requests
  made during the outage are missing, because the services do not buffer spans they cannot export.
  The scenario asserts that loss instead of hiding it.

### `trace-storage-outage`

*Storage fails; nothing is lost.*

- **Setup:** OpenSearch is scaled to zero for about 50 seconds; 15 orders are sent while it is down.
- **Expected:** every order completes.
- **Measured:** 15 × 201; every trace from the outage was stored completely after recovery; the
  Collector's storage queue peaked at 12 of 200 batches.
- **Telemetry proof:** the queue held data during the outage (the writes were really blocked) but
  never filled up (nothing was dropped).

---

## Reading a run's output

```text
==> Run 20260918T001359Z-payment-retry-cd2d: PASSED
    requests: 4 {201: 4}; orders read back: 4
[ OK ] [application] HTTP statuses: 201x4
[ OK ] [application] order states: Completedx4
[ OK ] [application] payment authorizations: 4 orders with exactly 1 authorization(s)
[ OK ] [telemetry] trace integrity: 4 trace(s) as expected
[ OK ] [telemetry] kubernetes metadata: 4 trace(s) as expected
[ OK ] [telemetry] correlation: 4 trace(s) as expected
[ OK ] [telemetry] sensitive data: 4 trace(s) as expected
[ OK ] [telemetry] retry attempts: 4 trace(s) as expected
[ OK ] [telemetry] idempotent replay: 4 trace(s) as expected
[ OK ] [telemetry] accumulated latency: 4 trace(s) as expected
[ OK ] [telemetry] state events: 4 trace(s) as expected
    record: <repository>\.runs\20260918T001359Z-payment-retry-cd2d\record.json
```

| Part | Meaning |
| --- | --- |
| `[application]` checks | HTTP answers, final order states, compensation and payment ledger compared with the scenario's expectations |
| `[telemetry]` checks | Stored traces and metrics compared with what really happened ([telemetry-contracts.md](telemetry-contracts.md)) |
| `record.json` | Every request (status, trace id, correlation id), every order, every check and every Kubernetes action of the run |

`uv run scenarioctl report` prints the latest run again; `uv run scenarioctl verify` repeats its
telemetry checks.

## Related documents

- [Writing scenarios](writing-scenarios.md) — adding an experiment
- [Telemetry contracts](telemetry-contracts.md) — how the telemetry checks work
- [Fault injection](fault-injection.md) — the fault modes the scenarios use
- [Experiment safety](experiment-safety.md) — how the platform is protected and restored
