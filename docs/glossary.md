# Glossary

Plain-language explanations of the terms used throughout this documentation. Each entry says what
the term means in general and, where useful, how it appears in this platform.

---

**Attribute**
A key–value pair attached to a span, such as `order.id = 0f3…` or `http.response.status_code = 201`.
Attributes are what makes a trace searchable. See [tracing.md](tracing.md).

**Baggage**
Values that travel with a request from service to service, next to the trace context. The platform
allows exactly three baggage keys — `correlation.id`, `scenario.id` and `scenario.run_id` — and
discards anything else a caller sends. See [tracing.md](tracing.md).

**Business rejection**
A request that was refused for a legitimate business reason: insufficient stock, a risk limit, a
declined card, a region that cannot be shipped to. It returns HTTP 422 and is deliberately **not**
marked as an error in traces. See [transaction-flow.md](transaction-flow.md).

**Canary value**
A harmless fake secret (a made-up API key) that every experiment sends with its requests. If it ever
appears in stored telemetry, some component recorded something it must not record, and the check
fails. See [telemetry-contracts.md](telemetry-contracts.md).

**Circuit breaker**
A switch in front of a dependency that "opens" after too many recent failures and then fails calls
immediately instead of waiting for a dependency that is known to be broken. It "closes" again after a
pause. See [resilience.md](resilience.md).

**Compensation**
Undoing an earlier step of a transaction after a later step failed — for example voiding a payment
and releasing reserved stock when shipping fails. It replaces the "rollback" of a database
transaction, which does not exist across services. See [transaction-flow.md](transaction-flow.md).

**Correlation id**
A short, human-friendly id for one request (header `X-Correlation-ID`). Kong generates one when the
caller does not send one, and it appears in responses, spans and logs.

**DB-less (Kong)**
Kong running without its own database: its configuration comes entirely from Kubernetes resources,
written by the Kong Ingress Controller. See [gateway.md](gateway.md).

**Decision wait**
How long the Collector waits after the first span of a trace arrives before deciding whether to keep
the trace: 20 seconds here. See [sampling.md](sampling.md).

**Default deny**
A network policy that blocks all traffic in a namespace until specific connections are allowed. Every
namespace of the platform starts this way. See [network-policies.md](network-policies.md).

**Downward API**
A Kubernetes feature that lets a pod read facts about itself — its own name, node and namespace —
as environment variables. The services use it to label every span with where it came from.

**Experiment lock**
A cluster-wide lock (a Kubernetes Lease) that ensures only one experiment, validation run or
deployment happens at a time, so exact counts stay exact. See
[experiment-safety.md](experiment-safety.md).

**Fault rule**
An instruction sent to one pod to misbehave in a controlled way — be slow, fail, time out or report
itself unready — for a limited time. See [fault-injection.md](fault-injection.md).

**Gateway API**
The Kubernetes standard for describing how traffic enters a cluster (`GatewayClass`, `Gateway`,
`HTTPRoute`), the successor of the older `Ingress` resource. See [gateway.md](gateway.md).

**Idempotency key**
A unique key sent with a request (header `Idempotency-Key`) so that repeating the request has no
additional effect. PaymentService uses it to make sure a retried payment is never charged twice. See
[resilience.md](resilience.md).

**Journal**
A file that records every change an experiment is *about to* make, before it makes it. After a
crash, the journal says exactly what must be undone. See [experiment-safety.md](experiment-safety.md).

**Kind (Kubernetes in Docker)**
A tool that runs a Kubernetes cluster inside Docker containers on one machine. The platform's cluster
has three kind nodes.

**Management port**
The second port of every service (8081), which serves health checks and operator endpoints. It is
never reachable through a Kubernetes Service or the gateway. See [security.md](security.md).

**Network identity**
The label `txplatform.io/network-identity` that tells the network policies what a pod is allowed to
talk to. See [network-policies.md](network-policies.md).

**NetworkPolicy**
A Kubernetes resource that allows or blocks traffic between pods, like a firewall rule written in
terms of labels instead of IP addresses.

**OTLP (OpenTelemetry Protocol)**
The standard protocol for sending traces and metrics. The .NET services send OTLP over gRPC (port
4317); Kong sends OTLP over HTTP (port 4318).

**Pod Security Standard (restricted)**
The strictest built-in Kubernetes level for pod security: containers must run as non-root users,
cannot gain privileges and drop all special Linux capabilities. All three platform namespaces enforce
it. See [security.md](security.md).

**Port-forward**
A temporary tunnel from a local port on the workstation into a pod or Service, created with
`kubectl port-forward`. It is how operators reach cluster-internal tools such as Jaeger and
Prometheus.

**Readiness probe**
A regular check Kubernetes performs to decide whether a pod may receive traffic. A pod that fails it
keeps running but is removed from its Service until it recovers.

**RED metrics**
The three basic health metrics of a service: **R**ate (requests per second), **E**rrors (failed
requests) and **D**uration (how long requests take). The Collector derives them from spans. See
[metrics-and-monitoring.md](metrics-and-monitoring.md).

**Retry**
Sending a failed call again, in the hope that the next attempt succeeds. Only failures that can
improve by waiting (timeouts, connection failures, 502/503/504) are retried here. See
[resilience.md](resilience.md).

**Root span**
The first span of a trace, the one without a parent. In this platform it is always Kong's `kong`
span.

**Scenario**
A defined experiment: which faults to inject, which traffic to send, and what the application and
the telemetry must show afterwards. There are 18. See [scenario-catalog.md](scenario-catalog.md).

**Span**
One timed operation inside a trace — for example "OrderService handled POST /orders" or "the
payment step". Spans have a name, a start and end time, attributes, events and a parent.

**Span event**
A timestamped note inside a span, such as `order.state_changed` from `Pending` to
`InventoryReserved`, or `retry` with the reason for the retry.

**Span metrics**
Request counts and duration histograms calculated by the Collector from spans. Because they are
computed before sampling, they include traffic whose traces were not stored.

**Tail sampling**
Deciding which traces to store *after* seeing them (errors, slow requests, flagged requests, plus a
fixed share of the rest), instead of deciding blindly when a request starts. See
[sampling.md](sampling.md).

**Telemetry contract**
The set of checks a scenario runs against the stored traces and metrics to prove they match what
really happened. See [telemetry-contracts.md](telemetry-contracts.md).

**Trace**
The complete record of one request as it travelled through the system: a tree of spans sharing one
trace id.

**Trace context (`traceparent`)**
The W3C-standard header that carries the trace id and the parent span id from one service to the
next, so all spans of a request join the same trace.

**TTL (time to live)**
How long something stays valid. Every fault rule has a TTL of at most an hour (30 minutes in scenario
files), after which it disappears on its own.
