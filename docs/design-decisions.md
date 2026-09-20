# Design decisions

This document records the important design decisions of the platform. Each one states what was
chosen, why, which alternatives were considered, and what the choice costs. Together they explain why
the platform looks the way it does.

---

## Overview

| # | Decision | Main trade-off |
| --- | --- | --- |
| 1 | [The gateway starts every trace](#1-the-gateway-starts-every-trace) | Clients cannot continue their own traces into the platform |
| 2 | [Tail sampling in the Collector](#2-tail-sampling-in-the-collector) | Memory in the Collector; one Collector replica |
| 3 | [Metrics derived from spans, before sampling](#3-metrics-derived-from-spans-before-sampling) | Cardinality must be controlled deliberately |
| 4 | [An orchestrated transaction with compensation](#4-an-orchestrated-transaction-with-compensation) | OrderService is the single coordinator; compensation is best effort |
| 5 | [Idempotency keys for payments](#5-idempotency-keys-for-payments) | PaymentService keeps state and must run one replica |
| 6 | [Resilience per dependency, not globally](#6-resilience-per-dependency-not-globally) | More settings to reason about |
| 7 | [Faults injected inside the services](#7-faults-injected-inside-the-services) | Experiment code inside production-shaped services |
| 8 | [Deterministic faults](#8-deterministic-faults) | Rare interleavings are not explored |
| 9 | [Telemetry verified by contracts](#9-telemetry-verified-by-contracts) | Contracts are code to maintain |
| 10 | [Locked and journaled experiments](#10-locked-and-journaled-experiments) | Experiments cannot run in parallel |
| 11 | [Two ports per service](#11-two-ports-per-service) | Operators need port-forwarding |
| 12 | [Gateway API with a DB-less Kong](#12-gateway-api-with-a-db-less-kong) | Kong plugins remain Kong-specific; Kong is pinned to its last open-source image |
| 13 | [Strict Kubernetes defaults](#13-strict-kubernetes-defaults) | Third-party charts need explicit security settings |
| 14 | [Durable trace writes](#14-durable-trace-writes) | Lower write throughput; queue memory |
| 15 | [Content-hashed images and one version manifest](#15-content-hashed-images-and-one-version-manifest) | Images accumulate locally |
| 16 | [A tested automation library with two tools](#16-a-tested-automation-library-with-two-tools) | A second language in the repository |

---

## 1. The gateway starts every trace

**Decision.** Kong never continues a trace sent by a client. It ignores incoming `traceparent`,
removes `tracestate` and `baggage`, and starts a new trace for every request.

**Why.** Otherwise trace identity would be controlled by callers: a client could attach its requests
to someone else's trace, provoke id collisions, or flag its own traffic so that it is always stored.
With the gateway deciding, every trace has exactly one root and a known origin.

**Alternatives.** Continue client traces (the common default); let OrderService decide instead of
the gateway.

**Trade-offs.** A caller's own trace cannot be continued into the platform. The link across that
boundary is the `traceresponse` header and `X-Correlation-ID` ([gateway.md](gateway.md)).

## 2. Tail sampling in the Collector

**Decision.** The services record and export every span. The Collector waits 20 seconds after a
trace's first span, then keeps every error, every trace of one second or more, every trace flagged
for debugging, and 25% of the rest.

**Why.** The interesting traces — failures and slow requests — are rare. Deciding at the start of a
request (head sampling) would drop them as often as the ordinary ones.

**Alternatives.** Head sampling in the services; storing everything.

**Trade-offs.** The Collector holds up to 20 000 undecided traces in memory, and all spans of a trace
must reach the same Collector, so it runs as a single replica ([sampling.md](sampling.md)).

## 3. Metrics derived from spans, before sampling

**Decision.** The Collector turns every span into request, error and duration metrics before the
sampling stage. No service emits its own metrics.

**Why.** Counts must be exact even for requests whose traces are dropped — "every one of the 76
rejected requests was counted" is only true if counting happens before sampling. One instrumentation
serves both traces and metrics, including for the gateway.

**Alternatives.** Metrics libraries in each service (duplicate instrumentation, no gateway view);
metrics computed from stored traces (they would count only the sampled ones).

**Trade-offs.** Metric labels must be chosen with care: identifiers stay on spans, pods are
excluded, and a hard limit caps the number of series
([metrics-and-monitoring.md](metrics-and-monitoring.md)).

## 4. An orchestrated transaction with compensation

**Decision.** OrderService drives each order through an explicit state machine: inventory and fraud
in parallel, then payment, then shipping. When a step fails, completed steps are undone in reverse
order.

**Why.** The platform's subject is making a distributed transaction's behavior visible. With one
orchestrator, every state change, failure and undo step is recorded in one place and appears in one
trace.

**Alternatives.** Choreography (services reacting to each other's events); two-phase commit.

**Trade-offs.** OrderService is the single coordinator, and compensation is best effort: an undo
call gets two attempts, and if both fail the failure is recorded and visible, but nothing retries it
later ([transaction-flow.md](transaction-flow.md)).

## 5. Idempotency keys for payments

**Decision.** Every payment authorization carries an idempotency key derived from the order
(`order-{orderId}-authorize`). PaymentService stores the outcome of each key and answers repeats with
the stored result.

**Why.** Retries are unavoidable once replies can be lost. Without idempotency, a retry after a lost
reply charges the customer twice — the exact failure the `payment-retry` scenario provokes.

**Alternatives.** No retries for payments (fails orders unnecessarily); deduplication on the caller's
side only (cannot know whether the lost request took effect).

**Trade-offs.** PaymentService keeps state in memory, so it must run exactly one replica and loses
its records on restart ([resilience.md](resilience.md)).

## 6. Resilience per dependency, not globally

**Decision.** Each dependency has its own chain of stage budget, retries, circuit breaker and attempt
timeout, tuned to its role — fraud checks are cheap and fast, payments are slow and retried on 409.
Only timeouts, connection failures and 502/503/504 are retried, never 500.

**Why.** A single global policy is wrong for most dependencies at once. Per-dependency settings make
every failure path deliberate, and unit tests assert their relationships (for example, that every
stage budget leaves room for all attempts).

**Alternatives.** One shared policy; retries at the gateway.

**Trade-offs.** More settings to understand; they are documented in one table
([resilience.md](resilience.md)). The gateway never retries, so a `POST /orders` can never become two
orders.

## 7. Faults injected inside the services

**Decision.** Each dependency contains a small fault framework, controlled through its management
port.

**Why.** Only injection inside the application can target one operation, one phase (after the effect,
to simulate a lost reply), one replica and one experiment run, and answer exactly like a real
business rejection. That precision makes the scenarios provable rather than approximate.

**Alternatives.** A proxy or service mesh that disturbs traffic; a chaos tool that kills pods.

**Trade-offs.** Experiment code lives in production-shaped services. It is off by default, reachable
only through the management port, and absent entirely when disabled
([fault-injection.md](fault-injection.md)).

## 8. Deterministic faults

**Decision.** Faults behave the same on every run: every request, the first *N* attempts per key, or
every *N*th request.

**Why.** Scenarios assert exact numbers. Random faults would force vague assertions, and vague
assertions hide regressions.

**Alternatives.** Random failure rates.

**Trade-offs.** Rare interleavings are not explored systematically; the load scenarios contribute
some real-world variance.

## 9. Telemetry verified by contracts

**Decision.** Every scenario reads its traces and metrics back and compares them with what actually
happened. The checks themselves are tested against recorded real traces, and an end-to-end negative
control proves the verifier can still fail.

**Why.** Observability data can be wrong in ways no dashboard reveals: broken parent links, missing
spans, errors marked on business rejections, secrets in attributes. The platform treats telemetry
as a product with its own tests ([telemetry-contracts.md](telemetry-contracts.md)).

**Alternatives.** Inspecting dashboards by hand; asserting only that "a trace exists".

**Trade-offs.** The contracts are code that must evolve together with the services.

## 10. Locked and journaled experiments

**Decision.** A Kubernetes Lease serves as a cluster-wide lock; every change is written to a journal
before it is made; restoration is derived from the journal; every fault expires.

**Why.** Exact counter comparisons need exclusive use of the platform, and an interrupted experiment
must never leave the platform broken ([experiment-safety.md](experiment-safety.md)).

**Alternatives.** Trusting the operator to clean up; `try/finally` blocks alone, which do not survive
a killed process.

**Trade-offs.** Experiments, validations and deployments run one at a time — deliberately.

## 11. Two ports per service

**Decision.** The business API listens on 8080; health checks and the `/internal` operator endpoints
on 8081. Each port refuses the other's paths.

**Why.** No Service, route or network policy leads to 8081, so operator functions — faults, breaker
reset, the payment ledger — cannot be reached from the network even with a valid token.

**Alternatives.** Authenticated internal endpoints on the business port; no operator endpoints.

**Trade-offs.** Operators reach these endpoints through `kubectl port-forward`, which requires
Kubernetes credentials ([security.md](security.md)).

## 12. Gateway API with a DB-less Kong

**Decision.** Routing uses the Kubernetes Gateway API (GatewayClass, Gateway, HTTPRoute) implemented
by the Kong Ingress Controller, with Kong running without a database.

**Why.** The Gateway API separates what the platform owns (listener, certificate, which namespaces
may attach routes) from what the application owns (which paths it exposes, with which limits). A
DB-less Kong keeps its entire configuration in Kubernetes resources.

**Alternatives.** Classic `Ingress` resources; a service-mesh gateway.

**Trade-offs.** Kong's plugins remain Kong-specific resources, and Kong is pinned to 3.9.3, the last
open-source image ([versions-and-upgrades.md](versions-and-upgrades.md)). That version carries a
tracing defect on 5xx answers, which the trace checks recognize explicitly
([gateway.md](gateway.md#known-defect-a-missing-parent-span-on-5xx-responses)).

## 13. Strict Kubernetes defaults

**Decision.** All namespaces enforce the restricted Pod Security level; every namespace starts from
deny-all network policies; every connection is allowed explicitly by network identity.

**Why.** Security settings that are merely present can be wrong unnoticed; enforced settings fail
loudly. Third-party components are configured to comply rather than exempted, and validation checks
prove each control against the running cluster.

**Alternatives.** The `baseline` Pod Security level; no network policies.

**Trade-offs.** Upstream charts need explicit security settings in their values files, and a
default-deny policy turns a missing dependency into a timeout instead of a refused connection
([network-policies.md](network-policies.md#a-behavior-to-know-no-endpoints-means-a-timeout-not-a-refusal)).

## 14. Durable trace writes

**Decision.** The Collector writes to OpenSearch synchronously, retries failed writes for up to five
minutes, and buffers them in a bounded queue of 200 batches.

**Why.** An observability platform must not lose data silently during a storage outage. With
synchronous writes, a failure is reported to the exporter and retried; the bound keeps a long outage
from exhausting the Collector's memory. The `trace-storage-outage` scenario delivered every trace of
a one-minute outage.

**Alternatives.** Asynchronous writes through a client-side buffer, which acknowledge data before it
is stored; no retries.

**Trade-offs.** Lower write throughput, and queue memory (bounded) during outages
([telemetry-pipeline.md](telemetry-pipeline.md#storing-export-to-opensearch)).

## 15. Content-hashed images and one version manifest

**Decision.** Every external version is pinned once in `deploy/versions.yaml` (the Kubernetes node
image by digest). Service images are tagged with a hash of their sources.

**Why.** Pinned versions make every bootstrap reproducible. Content tags make deployments idempotent:
an unchanged service keeps its tag and is not restarted, which the `recovery` suite verifies.

**Alternatives.** Versions spread over scripts and charts; `latest` tags; git commit tags (which
change even when a service's sources do not).

**Trade-offs.** Old images accumulate in the local Docker; upgrades are deliberate edits
([versions-and-upgrades.md](versions-and-upgrades.md)).

## 16. A tested automation library with two tools

**Decision.** The automation is a Python library with two entry points: `platformctl` for the
lifecycle and `scenarioctl` for experiments.

**Why.** The automation contains real logic — restore plans, recovery decisions, statistical bands,
trace parsing, contract checks — and that logic deserves unit tests. Two tools keep destructive
lifecycle commands separate from experiments.

**Alternatives.** Shell scripts or a Makefile; an existing chaos-engineering framework.

**Trade-offs.** A second language in the repository and a Python environment, created automatically
by `uv` ([automation-cli.md](automation-cli.md)).

## Related documents

- [Architecture](architecture.md) — how the decisions fit together
- [Known limitations](known-limitations.md) — the consequences worth knowing
- [Production considerations](production-considerations.md) — what would change outside a workstation
