# Services

The application consists of five .NET 10 services. This document describes what each one does, the
state it keeps, the rules it applies, and the foundation they all share. How they cooperate on one
order is described in [transaction-flow.md](transaction-flow.md).

---

## At a glance

| Service | Role | Replicas | State kept in memory | Can fail with |
| --- | --- | --- | --- | --- |
| **OrderService** | Public API and orchestrator | 1 | Up to 20 000 orders | Any dependency failure, mapped to 422/502/503/504 |
| **InventoryService** | Reserves and releases stock | 1 | Stock levels, up to 50 000 reservations | `insufficient-stock`, `unknown-sku` |
| **FraudService** | Evaluates order risk | 2 | Nothing (stateless) | `amount_threshold`, `quantity_threshold` |
| **PaymentService** | Authorizes and voids payments | 1 | Up to 100 000 idempotency records, the payment ledger | `payment-declined`, key misuse, 409 while in progress |
| **ShippingService** | Creates shipments | 1 | Up to 50 000 shipments | `zone-not-serviceable` |

The rules are deliberately simple and deterministic: the platform needs predictable outcomes it can
verify, not a realistic business domain. Every limit is bounded so that long load tests cannot
exhaust a pod's memory — the oldest entries are forgotten first.

## The shared foundation

All five services are built on the same library, `src/ServiceDefaults`, so they behave identically
in the ways that matter for operation and observability:

| Concern | Behavior |
| --- | --- |
| **Two ports** | Business API on 8080; health checks and operator endpoints on 8081. A request on the wrong port is refused, so `/internal/...` is never reachable through a Service or the gateway ([security.md](security.md)). |
| **Health** | `/health/live` answers as long as the process runs; `/health/ready` reports whether the service can do its work (for OrderService: whether it can fetch Keycloak's signing keys). |
| **Tracing** | OpenTelemetry with the platform's conventions, baggage allow-list and Kubernetes metadata ([tracing.md](tracing.md)). Health and management requests produce no spans. |
| **Logging** | JSON to standard output, with the trace id on every line so a log line can be tied to its trace. |
| **Errors** | Every error answer uses the RFC 9457 "problem details" format, including the request's `traceId`. |
| **Fault injection** | A small framework that can make an operation slow, failing or unready on command — only when enabled ([fault-injection.md](fault-injection.md)). |
| **Seed data** | Catalog and stock levels are read from a mounted JSON file, so they are configuration, not code. |
| **Graceful shutdown** | A readiness check that turns unhealthy while the pod stops, and a 5-second pre-stop delay, so in-flight requests finish ([resilience.md](resilience.md)). |

## OrderService

**The public face of the platform and the only service Kong routes to.**

- Exposes `POST /orders` (requires the `orders.write` permission) and `GET /orders/{orderId}`
  (requires `orders.read`) — see [api-reference.md](api-reference.md).
- Validates every token and every request body before any work is done.
- Runs the transaction: inventory and fraud concurrently, then payment, then shipping; on failure it
  undoes earlier steps ([transaction-flow.md](transaction-flow.md)).
- Owns the resilience settings for every dependency: timeouts, retries, circuit breakers
  ([resilience.md](resilience.md)).
- Is the **correlation boundary**: it discards any baggage a caller sends, accepts only validated
  correlation and experiment ids, and returns the trace id in a `traceresponse` header.
- Honours `X-Debug-Trace: true` only for callers with the `traces.debug` permission.
- Prices orders from its catalog:

| SKU | Unit price (EUR) | Stock (InventoryService) | Purpose |
| --- | --- | --- | --- |
| `SKU-1001` | 12.50 | 1 000 000 | Ordinary orders |
| `SKU-1002` | 49.90 | 1 000 000 | Ordinary orders |
| `SKU-2001` | 950.00 | 1 000 000 | Six of them exceed the fraud limit |
| `SKU-9000` | 5.00 | **0** | Always out of stock |

## InventoryService

**Reserves stock for an order and releases it again.**

| Operation | Endpoint | Result |
| --- | --- | --- |
| Reserve | `POST /inventory/reservations` | 201 reserved; 200 if this order already holds a reservation; 422 `insufficient-stock` or `unknown-sku` |
| Release | `DELETE /inventory/reservations/{orderId}` | 200 released; 404 if there was nothing to release |

- Reservations are **all or nothing**: a multi-line order either reserves every line or none.
- Reserving twice for the same order returns the existing reservation — safe to retry.
- Releasing is safe to repeat; OrderService treats "nothing to release" as a successful undo.

## FraudService

**Decides whether an order is too risky.**

| Rule | Result |
| --- | --- |
| Order total above 5 000 | Rejected, rule `amount_threshold` (risk score 90) |
| More than 60 items | Rejected, rule `quantity_threshold` (risk score 80) |
| Otherwise | Approved, rule `within_limits`, with a risk score below 80 |

The answer is always HTTP 200 with a `decision` of `approved` or `rejected`; OrderService turns a
rejection into a business rejection of the order. FraudService keeps no state, which is why it can
run **two replicas** spread across the two worker nodes — the basis for the scenarios that degrade or
disable a single replica.

## PaymentService

**Authorizes a payment exactly once, however often the request is repeated.**

| Operation | Endpoint | Result |
| --- | --- | --- |
| Authorize | `POST /payments/authorizations` with `Idempotency-Key` | 201 authorized; 422 declined; 409 while the same key is still being processed; 422 if the key is reused with a different request |
| Void | `POST /payments/voids` | 200 with `voided` or `not_found` |
| Ledger (management port) | `GET /internal/payments/{orderId}` | `{orderId, authorizations, voided}` |

Test payment tokens produce deterministic outcomes, the way real payment providers offer test cards:

| Token | Outcome |
| --- | --- |
| `tok_test_declined` | Declined (`card_declined`) |
| `tok_test_insufficient_funds` | Declined (`insufficient_funds`) |
| any other valid token | Approved |

The answer to an authorization is stored **before** it is sent, so a caller whose reply was lost gets
the same answer again on retry instead of a second charge ([resilience.md](resilience.md)).

PaymentService runs **exactly one replica** on purpose: its idempotency records live in memory, and
two replicas would each keep their own, which would break the "exactly once" guarantee. A production
deployment would use a shared, durable store ([production-considerations.md](production-considerations.md)).

## ShippingService

**Creates the shipment for an order.**

| Zone | Result |
| --- | --- |
| `domestic`, `international` | 201 shipment created (200 if it already exists for this order) |
| `restricted` | 422 `zone-not-serviceable` |

Creating a shipment twice for the same order returns the existing shipment, so retries are safe.

## Resources

Every container declares its CPU and memory requests and limits; a validation check fails if a
workload ever relies on namespace defaults instead.

| Service | Requests (CPU / memory) | Limits (CPU / memory) |
| --- | --- | --- |
| OrderService | 100m / 128 MiB | 1 CPU / 256 MiB |
| Inventory, Fraud, Payment, Shipping | 50m / 96 MiB | 500m / 192 MiB |

## Related documents

- [Transaction flow](transaction-flow.md) — how the services cooperate on one order
- [API reference](api-reference.md) — every endpoint with its requests and answers
- [Resilience](resilience.md) — how OrderService protects itself from its dependencies
- [Codebase guide](codebase-guide.md) — where each service's code lives
