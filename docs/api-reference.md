# API reference

The platform exposes three groups of HTTP APIs:

| Group | Reachable from | Purpose |
| --- | --- | --- |
| **Order API** | The workstation, through Kong at `https://localhost:8443` | Create and read orders |
| **Service APIs** | Only OrderService, inside the cluster | The four dependencies' business operations |
| **Management API** | Only `kubectl port-forward` to a pod's port 8081 | Health, fault rules, circuit breakers, payment ledger |

All bodies are JSON. All error answers use the "problem details" format (RFC 9457).

---

## Order API

### Authentication

Every request needs an access token from Keycloak in the `Authorization: Bearer <token>` header.
Tokens are obtained with the OAuth 2.0 client-credentials flow; the step-by-step commands are in the
[walkthrough](walkthrough.md#step-2--send-one-order-by-hand), and the realm, clients and permissions
in [identity-and-tokens.md](identity-and-tokens.md).

| Operation | Required permission |
| --- | --- |
| `POST /orders` | `orders.write` |
| `GET /orders/{orderId}` | `orders.read` |
| `X-Debug-Trace: true` honoured | `traces.debug` |

### `POST /orders` — create an order

Runs the complete order transaction and answers when it has finished
([transaction-flow.md](transaction-flow.md)).

**Request body**

```json
{
  "items": [
    { "sku": "SKU-1001", "quantity": 2 }
  ],
  "paymentToken": "tok_test_approved",
  "shippingZone": "domestic"
}
```

| Field | Type | Rules |
| --- | --- | --- |
| `items` | array | 1 to 20 lines; each SKU at most once |
| `items[].sku` | string | Matches `SKU-` followed by four digits, and exists in the catalog |
| `items[].quantity` | integer | 1 to 100 |
| `paymentToken` | string | An opaque token: `tok_` followed by 3–64 lowercase letters, digits or underscores. A token containing 12 to 19 consecutive digits is refused, so a card number can never enter the platform |
| `shippingZone` | string | `domestic`, `international` or `restricted` |

The catalog, test tokens and business rules are listed in [services.md](services.md).

**Optional request headers**

| Header | Meaning |
| --- | --- |
| `X-Correlation-ID` | Your own id for the request (letters, digits and `. _ : -`, up to 128 characters). Kong generates one when it is missing |
| `X-Scenario-Id`, `X-Scenario-Run-Id` | Experiment labels; they are carried to every service and recorded on spans |
| `X-Debug-Trace: true` | Keep this request's trace regardless of sampling. Ignored unless the token holds `traces.debug` |

**Responses**

| Status | Meaning | Answered by |
| --- | --- | --- |
| **201 Created** | The order completed. `Location` points to it | OrderService |
| **400 Bad Request** | The body is invalid; `errors` lists every problem | OrderService |
| **401 Unauthorized** | Missing, expired or invalid token | OrderService |
| **403 Forbidden** | Valid token without the required permission | OrderService |
| **413 Payload Too Large** | Body larger than 16 KB | Kong |
| **422 Unprocessable Content** | A dependency refused the order for a business reason | OrderService |
| **429 Too Many Requests** | More than 30 requests per second, or 1 500 per minute, from this client | Kong |
| **502 Bad Gateway** | A dependency answered with a server error | OrderService |
| **503 Service Unavailable** | A dependency could not be reached, or its circuit breaker is open | OrderService |
| **504 Gateway Timeout** | A dependency did not answer in time | OrderService |

**Example — completed order (201)**

```json
{
  "orderId": "01a0b3d0-b6b3-79ac-a806-f39debefd144",
  "state": "Completed",
  "totalAmount": 12.5,
  "currency": "EUR",
  "shippingZone": "domestic",
  "lines": [
    { "sku": "SKU-1001", "quantity": 1, "unitPrice": 12.5, "lineTotal": 12.5 }
  ],
  "failure": null,
  "compensation": [],
  "stateHistory": ["Pending", "InventoryReserved", "PaymentAuthorized", "ShipmentCreated", "Completed"],
  "createdAt": "2026-09-18T09:19:55.0596503+00:00",
  "updatedAt": "2026-09-18T09:19:55.3121964+00:00"
}
```

**Example — invalid request (400)**

```json
{
  "type": "https://txplatform.local/problems/invalid-order",
  "title": "The order is invalid",
  "status": 400,
  "errors": {
    "items[0].sku": ["must look like SKU-1234"],
    "items[0].quantity": ["must be between 1 and 100"],
    "paymentToken": ["must be an opaque token such as tok_test_approved"],
    "shippingZone": ["must be one of: domestic, international, restricted"]
  },
  "traceId": "00-783eef49e00b59e2effb432f608e594e-78e575445b2115f4-01"
}
```

**Example — business rejection (422)**

A failed order still exists: its id, final state, the failure and the compensation performed are
part of the answer, and `Location` points to it.

```json
{
  "type": "https://txplatform.local/problems/order-rejected",
  "title": "The order ended in state InventoryFailed",
  "status": 422,
  "detail": "inventory failed: insufficient-stock",
  "orderId": "01a0b3d0-bfeb-78ea-bd6e-5c179656248b",
  "state": "InventoryFailed",
  "failure": { "stage": "inventory", "kind": "business_rejection", "reason": "insufficient-stock" },
  "compensation": [],
  "traceId": "00-06e32aad4586b5f50e5762b5307c56d2-cef1aac836cf54d2-01"
}
```

**Example — technical failure (504)**

```json
{
  "type": "https://txplatform.local/problems/order-dependency-failure",
  "title": "The order ended in state PaymentFailed",
  "status": 504,
  "detail": "payment failed: timeout",
  "state": "PaymentFailed",
  "failure": { "stage": "payment", "kind": "timeout", "reason": "timeout" },
  "compensation": [
    { "action": "VoidPayment", "status": "Succeeded", "detail": "done" },
    { "action": "ReleaseInventory", "status": "Succeeded", "detail": "done" }
  ]
}
```

### `GET /orders/{orderId}` — read an order

Returns the order in the same shape as a 201 response, whatever its final state. Answers 404 with
problem type `order-not-found` for an unknown id. Orders are kept in memory (up to 20 000), so they
disappear when OrderService restarts.

### Response headers

| Header | Set by | Meaning |
| --- | --- | --- |
| `X-Correlation-ID` | Kong (echoed by OrderService) | The request's correlation id |
| `traceresponse` | OrderService | W3C trace response: `00-<trace id>-<span id>-<flags>`; the trace id finds the trace in Jaeger |
| `X-Kong-Request-Id` | Kong | Kong's own request id, also recorded on the spans |
| `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` | Kong | The per-second rate limit and what is left of it |
| `X-RateLimit-Limit-Second`, `X-RateLimit-Limit-Minute` (and `-Remaining-`) | Kong | Both rate limit windows |
| `Location` | OrderService | `https://localhost:8443/orders/<order id>` for created and failed orders |

### Problem types

| `type` (suffix of `https://txplatform.local/problems/`) | Status | When |
| --- | --- | --- |
| `invalid-order` | 400 | Request validation failed |
| `order-rejected` | 422 | A business rejection ended the order |
| `order-dependency-failure` | 502, 503, 504 | A technical failure ended the order |
| `order-not-found` | 404 | Unknown order id |

The `failure.kind` values are `business_rejection`, `dependency_error`, `timeout` and `unavailable`;
the `failure.stage` values are `inventory`, `fraud`, `payment` and `shipping`.

---

## Service APIs

These run on port 8080 of each dependency. Only OrderService may reach them — the network policies
enforce it — so they take no credentials of their own ([network-policies.md](network-policies.md)).

| Service | Endpoint | Request body | Answers |
| --- | --- | --- | --- |
| Inventory | `POST /inventory/reservations` | `{orderId, lines: [{sku, quantity}]}` | 201 reserved · 200 already reserved · 422 `insufficient-stock` / `unknown-sku` |
| Inventory | `DELETE /inventory/reservations/{orderId}` | – | 200 released · 404 nothing to release |
| Fraud | `POST /fraud/evaluations` | `{orderId, totalAmount, itemCount}` | 200 `{orderId, decision, rule, riskScore}` with `decision` `approved` or `rejected` |
| Payment | `POST /payments/authorizations` + header `Idempotency-Key` | `{orderId, amount, currency, paymentToken}` | 201 authorized · 422 declined or key misused · 409 same key still in progress · 400 key missing |
| Payment | `POST /payments/voids` | `{orderId}` | 200 `{orderId, status}` with `voided` or `not_found` |
| Shipping | `POST /shipments` | `{orderId, zone, itemCount}` | 201 created · 200 already created · 422 `zone-not-serviceable` |

OrderService sends an `X-Attempt` header with every call (1, 2, 3…), which PaymentService records
on its spans. A replayed payment answer carries `Idempotent-Replayed: true`.

---

## Management API (port 8081)

Every service serves operator endpoints on its second port. No Service, route or gateway points at
it, and the services refuse these paths on the business port ([security.md](security.md)). Reach it
with a port-forward:

```bash
kubectl -n transaction-platform port-forward pod/<pod name> 8081:8081
```

| Endpoint | Services | Purpose |
| --- | --- | --- |
| `GET /health/live` | all | Liveness: the process is running |
| `GET /health/ready` | all | Readiness: the service can do its work (OrderService also checks Keycloak's signing keys) |
| `GET /internal/faults` | all | List the active fault rules of this pod |
| `PUT /internal/faults` | all | Add a fault rule ([fault-injection.md](fault-injection.md)) |
| `DELETE /internal/faults` | all | Remove every fault rule of this pod; answers `{"removed": n}` |
| `DELETE /internal/faults/{id}` | all | Remove one fault rule |
| `POST /internal/resilience/reset` | OrderService | Close every circuit breaker; answers `{"closedBreakers": n}` |
| `GET /internal/payments/{orderId}` | PaymentService | Ledger summary `{orderId, authorizations, voided}` |

The fault endpoints exist only when fault injection is enabled, which the local deployment does.

## Related documents

- [Transaction flow](transaction-flow.md) — what happens during `POST /orders`
- [Identity and tokens](identity-and-tokens.md) — obtaining and validating tokens
- [Gateway](gateway.md) — the rate and size limits that produce 429 and 413
- [Services](services.md) — the catalog and the business rules behind 422 answers
