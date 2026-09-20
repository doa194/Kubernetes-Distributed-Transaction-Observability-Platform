# Known limitations

The platform is built to be honest about what it shows, and that includes its own limits. This
document lists the behaviors and constraints worth knowing before trusting a trace, reading a
measurement, or reusing a configuration elsewhere — what each one means and how the platform deals
with it. What would change in a production setting is described separately in
[production-considerations.md](production-considerations.md).

---

## Telemetry

### Failed orders show a gap below the gateway

**What happens.** When OrderService answers with a status of 500 or above, Kong 3.9.3 marks its call
as failed and then exports a *new* `kong.balancer` span instead of the one whose id it had already
passed to OrderService in `traceparent`. OrderService's span therefore refers to a parent that never
arrives, and Jaeger shows the trace in two parts under one trace id.

**Effect.** Every order that fails with 502, 503 or 504 has one broken parent link at the gateway.
The rest of the trace is complete, and every span still carries the same trace id.

**How it is handled.** The trace-integrity check accepts exactly this pattern — Kong's single
balancer span carries an error status and a 5xx answer, and the orphan is OrderService's server span
for the same Kong request — and reports it as a visible note on the run. Any other missing parent
still fails. Kong keeps `retries` at 0 regardless: retries at the gateway could duplicate a
`POST /orders` ([gateway.md](gateway.md#known-defect-a-missing-parent-span-on-5xx-responses)).

### Spans are lost while the Collector is unavailable

**What happens.** The services export spans in the background and do not keep spans they could not
deliver. Kong retries its exports for a while, so some gateway spans may still arrive later.

**Effect.** Requests served during a Collector outage have no service spans; their traces are absent
or contain only Kong's spans. Orders themselves are unaffected.

**How it is handled.** The `collector-outage` scenario asserts the loss — it would fail if the traces
of the outage window were complete, because then the outage would not have happened. The gap is
visible, not hidden.

### One Collector, by necessity

**What happens.** Tail sampling needs all spans of a trace in one process, so the Collector runs as a
single replica with the `Recreate` update strategy.

**Effect.** A Collector restart means a short gap in ingestion, and its counters (self-metrics and
span metrics) start again from zero. Prometheus's `rate()` and `increase()` handle such resets.

### Metrics are per service, not per pod

**What happens.** Span metrics deliberately exclude pod names and instance ids, so restarts do not
create new time series.

**Effect.** "Which replica is slow?" is answered by traces (every span names its pod), not by
metrics — as the `replica-degradation` scenario demonstrates.

### Kong's spans carry no pod name

Kong's OpenTelemetry plugin is configured statically and cannot know which pod it runs in. Kong spans
carry service, namespace and deployment; the .NET services' spans also carry pod and node.

### Stored traces are kept for two days

Retention deletes daily indices older than two days, and there is no archive. Re-running a scenario
reproduces its evidence ([trace-storage.md](trace-storage.md)).

### Client addresses are pseudonymized, not anonymized

Kong's span stores a SHA-256 hash of the caller's IP address. Equal addresses produce equal hashes,
which is useful for grouping, but an IPv4 address can be recovered by hashing all four billion
candidates ([telemetry-pipeline.md](telemetry-pipeline.md#5-hash-the-client-address)).

## Transaction and data

| Limitation | Effect |
| --- | --- |
| **All business data is in memory.** Orders, stock reservations, payments and shipments live in the services' memory | A restarted pod starts empty: orders can no longer be read back, and the payment ledger is reset. This is why `pod-deletion` does not check the ledger |
| **Nothing resumes an interrupted transaction.** The transaction runs inside OrderService | If OrderService dies in the middle of an order, earlier steps are not undone |
| **Compensation is best effort.** An undo call gets two attempts within 1.2 seconds | A failed undo is recorded on the order and in the trace, but nothing retries it later |
| **PaymentService must run exactly one replica** | Its idempotency records are in memory; a second replica would not know the first one's records |
| **Circuit-breaker state is per OrderService pod** | With several OrderService replicas, each would learn about a failing dependency separately |

See [transaction-flow.md](transaction-flow.md#guarantees-and-their-limits) and
[resilience.md](resilience.md#limits).

## Availability

| Limitation | Effect |
| --- | --- |
| **Most components run one replica.** Only FraudService runs two | A pod replacement is covered by the pre-stop delay and retries (240 of 240 orders survived a rolling restart, 80 of 80 a pod deletion), but a crashed single replica is an outage for its function |
| **A missing dependency times out instead of being refused.** The default-deny network policy drops connections to a Service without endpoints | The failure is detected after the attempt timeouts (about 3.5 s, answer 504) instead of immediately (503) ([network-policies.md](network-policies.md#a-behavior-to-know-no-endpoints-means-a-timeout-not-a-refusal)) |
| **Rate limits are counted per Kong pod** | Exact with one Kong replica; with several, each would count separately |

## Security

| Limitation | Effect |
| --- | --- |
| **Plain HTTP inside the cluster** | Traffic between pods is not encrypted; network policies are the only boundary |
| **Dependencies accept calls without authentication** | Only the network policy decides that OrderService alone may call them |
| **Keycloak stores its data on a temporary volume** | When its pod is replaced, the realm is imported again and signing keys change; earlier tokens become invalid |
| **Client secrets do not rotate** | They are generated once and kept until the Secret is deleted |
| **OpenSearch has no authentication or TLS** | Any workload the network policy admitted could read or delete traces |

The full list, with the production counterpart of each, is in [security.md](security.md#local-simplifications).

## Environment

| Limitation | Effect |
| --- | --- |
| **Kong is pinned to 3.9.3**, the last open-source Kong Gateway image | Moving on means a licensed image or a different gateway ([versions-and-upgrades.md](versions-and-upgrades.md)) |
| **Kubernetes is pinned to 1.36** | The Kong Ingress Controller 3.5 supports Kubernetes up to 1.36 |
| **Verified on Windows 11 with Docker Desktop** | Docker, kind, Helm, .NET and Python also run on Linux and macOS, but the platform has not been verified there |
| **A single workstation** | The platform needs about 8 GB of memory for Docker; capacity and throughput limits are not measured |

## Tooling behavior worth knowing

| Behavior | Symptom | What to do |
| --- | --- | --- |
| `kubectl` installed through a launcher (for example Chocolatey's) starts the real program as a child process | Stopping only the launcher leaves a port-forward running | The automation stops whole process trees; stop manual port-forwards with Ctrl+C |
| `dotnet test` uses Microsoft.Testing.Platform (set in `global.json`) | `dotnet test --nologo` reports "Zero tests ran" and exits with code 5 | Run plain `dotnet test` |
| curl on Windows checks certificate revocation | `curl: (60) schannel: the revocation status is unknown` | Add `--ssl-revoke-best-effort` |
| A rate needs two data points | Jaeger's Monitor tab and new PromQL series stay empty for about 30 seconds | Wait; the automated checks poll instead of failing |
| Jaeger's index cleaner selects indices by creation time | An index created today is kept even if its name carries an old date | Expected; retention tests use a throwaway prefix instead |

## Related documents

- [Production considerations](production-considerations.md) — what would change for production use
- [Design decisions](design-decisions.md) — the choices behind these limits
- [Troubleshooting](troubleshooting.md) — symptoms and fixes
