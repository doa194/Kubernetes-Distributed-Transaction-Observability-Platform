# Testing strategy

The platform is verified at seven layers, from millisecond unit tests to experiments that delete
pods in a running cluster. Each behavior is tested at the **cheapest layer that can prove it
reliably**, and a behavior appears at a second layer only when that layer protects against a
different kind of failure. This document explains the layers, what lives in each and why, and how
to run them.

---

## The layers

| Layer | Count | Runs against | Catches | Command |
| --- | --- | --- | --- | --- |
| .NET unit tests | 101 | Pure logic, no network | Wrong rules and decisions | `dotnet test` |
| .NET component tests | 30 | One service in memory, real HTTP pipeline | Broken API contracts of a single service | `dotnet test` |
| .NET integration tests | 20 | All five services over real HTTP; Keycloak in a container | Failures that only exist between services | `dotnet test` |
| Python unit tests | 121 | Automation logic; traces recorded from real runs | Wrong automation decisions and trace checks that cannot fail | `uv run pytest` |
| Operational validation | 42 checks (+5 disruptive) | The deployed cluster | Platform properties that only a real cluster has | `uv run platformctl validate` |
| Scenarios | 18 | The deployed cluster, with faults | Wrong behavior under failure, and telemetry that misreports it | `uv run scenarioctl run <id>` |
| End-to-end tests | 5 | The deployed cluster, through the tools | Broken critical journeys and a verifier that can no longer fail | `uv run pytest -m e2e` |

`dotnet test` reports the three .NET layers together (151 tests).

## One behavior, several layers — without duplication

Idempotent payments show how the layers divide the work. Each test answers a question the others
cannot:

| Layer | Test | The question only this layer answers |
| --- | --- | --- |
| Unit | `Concurrent_requests_with_the_same_key_start_exactly_once` and six more | Is the idempotency decision correct for every combination of key, payload and progress? |
| Component | `Retry_with_same_key_replays_the_first_authorization_without_charging_again` | Does PaymentService's HTTP API return the stored answer, with the right status and headers? |
| Integration | `Lost_payment_response_is_replayed_on_retry_instead_of_charging_twice` | Do OrderService's real retry pipeline and PaymentService's store work together when a reply is really lost over HTTP? |
| Scenario | `payment-retry` | Does it hold in the deployed cluster — and do the stored traces show both attempts and the replay? |

## What lives where

### .NET unit tests (`tests/Platform.UnitTests`)

Decisions that must never drift, tested without any host:

- the order state machine and transaction rules — which failure wins, which steps are compensated;
- request validation;
- retry classification — what is retried, what counts against a circuit breaker, whether every
  stage budget leaves room for all attempts;
- idempotency decisions for payments; idempotent inventory reservations and shipments;
- the fault framework — rule validation, expiry, scoping, phases, and every fault mode;
- the telemetry boundary — correlation-id validation and the baggage allow-list.

Time-dependent rules use a fake clock (`FakeTimeProvider`), so expiry and delays are tested
instantly and deterministically.

### .NET component tests (`tests/Platform.ComponentTests`)

One service started in memory with its real HTTP pipeline. They protect the **externally visible
contract** of a single service:

- the Order API: 201 with `Location` and state history, 400 problem details without any dependency
  call, 422 with the order reference, 404 for unknown orders;
- authorization: missing, insufficient and valid permissions; health endpoints need no token;
- the dependencies' APIs: idempotency keys, business rejections as problem details;
- the management API: fault rules change and restore responses, invalid rules get field errors,
  readiness loss keeps the pod alive, and the fault API **does not exist** when disabled.

### .NET integration tests (`tests/Platform.IntegrationTests`)

All five services run in the test process on real ports, wired together the way Kubernetes wires
them, so timeouts, retries and refused connections behave like real HTTP. They protect behavior that
only exists **between** services:

- inventory and fraud checks run concurrently; a completed order is authorized exactly once;
- retries with idempotent replay, including concurrent duplicate requests;
- compensation after payment and shipping failures, and a declined payment that releases stock
  without a void;
- a circuit breaker that opens and is closed again by the operator endpoint;
- trace structure across services: retried attempts as separate spans, injected faults marked on
  exactly the targeted spans, business rejections not marked as errors, no spans for health calls,
  no sensitive values on any span;
- a stopped dependency (connection refused) failing the order within its budget.

The **realm contract test** starts a real Keycloak with the repository's realm file
(Testcontainers) and checks that its tokens are accepted and authorized by OrderService's real token
validation. A wrong claim name or audience is exactly the kind of mistake a mocked token would hide.
These tests need Docker.

### Python unit tests (`automation/tests/unit`)

The automation's own logic: version pinning, the context guard, certificate decisions, pre-flight
rules, probe scripts, Helm recovery decisions, content-hash image tags, process-tree termination,
Prometheus selectors, statistics, OTLP encoding, scenario schema validation, workload schedules,
journal restore plans, lock expiry, expectation rules and trace parsing.

The **trace checks** are tested against traces recorded from real runs
(`automation/tests/fixtures/traces/`). Each test breaks exactly one detail of a real trace and
asserts that the right check complains ([telemetry-contracts.md](telemetry-contracts.md#the-checks-themselves-are-tested)).

### Operational validation

Properties that exist only in a real cluster: Pod Security admission, NetworkPolicy enforcement,
TLS at the edge, token issuance, trace storage and retention, sampling rates, span metrics, and the
independence of the telemetry components. Unit tests on YAML could not prove any of them — only the
platform can ([validation-suites.md](validation-suites.md)).

### Scenarios

Whole-system behavior under failure, verified in both directions: what the application did, and
whether the telemetry reports it truthfully ([scenario-catalog.md](scenario-catalog.md)).

### End-to-end tests (`automation/tests/e2e`)

Deliberately few, because the layers below already cover the details:

| Test | Protects |
| --- | --- |
| `normal-order` and `payment-timeout` pass end to end | The two critical journeys, through the real tools |
| The verifier rejects telemetry that does not match | A negative control: successful orders checked against the `payment-retry` contract must fail |
| A second experiment is refused while another process holds the lock | Experiment isolation, across real processes |
| A killed run is restored by `reset` | Crash recovery: a run is killed after scaling ShippingService to zero, and `reset` must restore it |

## Rules the tests follow

| Rule | In practice |
| --- | --- |
| **Cheapest reliable layer** | Retry classification is a unit test, not an integration test; NetworkPolicy behavior is a cluster check, not a unit test on YAML |
| **No configuration tests** | Nothing asserts that a file contains a value; the platform is asked whether the behavior exists |
| **Real dependencies where mocks would lie** | Real Keycloak for token contracts; real HTTP between services; recorded real traces for the trace checks |
| **Negative controls** | Mutated traces, a mismatched contract, a privileged pod that must be refused, a deny policy that must block, a disabled fault API that must not exist |
| **Deterministic by design** | Faults are deterministic (fail the first *N* attempts, not "10% randomly"); fake clocks for time-dependent rules |
| **Statistics only where the system is statistical** | The sampling rate is checked against a binomial band; everything else is exact |
| **Deadlines, not sleeps** | Checks that wait for the platform poll against a deadline and fail with a clear message |

## Running the tests

```bash
dotnet test                                    # 151 .NET tests (Docker required for the Keycloak contract test)
uv run pytest                                  # 121 Python unit tests
uv run platformctl validate                    # 42 checks against the running platform, about 5 minutes
uv run platformctl validate --suite observability-resilience --suite recovery   # 5 disruptive checks
uv run pytest -m e2e                           # 5 end-to-end tests against the running platform
for s in $(uv run scenarioctl list | cut -d' ' -f1); do uv run scenarioctl run "$s"; done   # about 23 minutes
```

Run `dotnet test` without extra options such as `--nologo`: they are passed on to the test
application, which then runs no tests and reports "Zero tests ran". The first two commands need no
cluster.

## What is not tested

| Area | Why |
| --- | --- |
| Throughput and latency limits | The platform is a local environment; `traffic-spike` checks behavior under a spike, not capacity |
| Multi-replica Kong, Keycloak, Collector or OpenSearch | They run as single replicas by design ([known-limitations.md](known-limitations.md)) |
| Operating systems other than Windows 11 with Docker Desktop | Every test above was run on that combination; the tools used also support Linux and macOS, but the platform has not been verified there |

## Related documents

- [Validation suites](validation-suites.md) — every operational check
- [Scenario catalog](scenario-catalog.md) — every experiment
- [Telemetry contracts](telemetry-contracts.md) — how telemetry is verified
- [Codebase guide](codebase-guide.md) — where the test projects live
