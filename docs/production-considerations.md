# Production considerations

The platform runs on a single workstation, and several of its choices are deliberate
simplifications for that setting. This document separates three things clearly:

- **Implemented now** — what the platform does today, as described in the other documents.
- **Simplified for local use** — where a local shortcut was chosen, and why.
- **Needed for production** — what a production system would do instead.

**Nothing in the "Production" columns below is implemented.** These are recommendations, not
features.

---

## What would carry over unchanged

Several properties are not simplifications; a production system would keep them as they are:

- The gateway owns the trace root, so trace identity cannot be influenced from outside.
- Span metrics are computed before sampling, so counts stay exact when traces are dropped.
- Business rejections are never marked as technical errors.
- Payments are idempotent, and the gateway never retries order creation.
- Operator functions live on a separate port that no route or Service exposes.
- Every namespace enforces the restricted Pod Security level and starts from deny-all networking.
- Telemetry is verified against what actually happened instead of being assumed correct.
- Every experimental change is journaled before it is made, and every fault expires.

## Application

| Implemented now | Why it is simplified | Production |
| --- | --- | --- |
| Orders, reservations, payments and shipments held in memory, with fixed capacities per pod | No database to deploy and explain; failures stay easy to see | A database per service; durable payment ledger and idempotency records |
| The transaction and its compensation run inside OrderService | Keeps the failure path readable in one place | A durable workflow (outbox pattern, saga orchestrator or workflow engine) that resumes after a crash |
| A failed undo step is recorded, not repaired | Nothing is hidden, but nothing heals itself | Retries with backoff, then a dead-letter queue and an operator task |
| Circuit-breaker state per OrderService pod | Matches the in-process resilience pipeline | Usually acceptable; otherwise shared state or a mesh-level breaker |
| Prices from a seeded catalog; test tokens decide payments | Deterministic experiments | A catalog or pricing service; a real payment provider |

## Identity and security

| Implemented now | Why it is simplified | Production |
| --- | --- | --- |
| Plain HTTP inside the cluster | One less moving part; network policies are the boundary | Mutual TLS between workloads (service mesh or application TLS) |
| Dependencies accept calls without authentication | The network policy already restricts the caller | Authenticated service-to-service calls (mTLS identities or tokens) |
| A local CA and a 90-day certificate for `localhost` | No external dependency | A managed CA with automated issuance and rotation (for example cert-manager) and real host names |
| Keycloak with a file database on a temporary volume, one replica | Fast start; the realm comes from a file | An external database, several replicas, backups, a real host name |
| Client secrets generated once, never rotated | Nothing to coordinate locally | Scheduled rotation through a secret manager |
| OpenSearch without authentication or TLS | Simplifies a single node | Authentication, TLS and per-client roles |
| Kubernetes Secrets without extra encryption | Adequate on a workstation | Encryption at rest with a key management service, or an external secret manager |
| Fault injection enabled | Experiments are the platform's purpose | Disabled outside test environments: with `faults.enabled=false` the endpoints do not exist |

## Telemetry

| Implemented now | Why it is simplified | Production |
| --- | --- | --- |
| One Collector replica | Tail sampling needs all spans of a trace in one process | Two tiers: receiving Collectors with a load-balancing exporter (by trace id) in front of several sampling Collectors |
| Services send spans directly to the Collector and drop what they cannot deliver | No extra component between services and Collector | A node-local agent or a message queue that buffers during Collector outages |
| Client IP addresses hashed with plain SHA-256 | Keeps grouping possible without storing addresses | A keyed hash or truncated addresses — a plain hash of an IPv4 address can be reversed by trying all addresses |
| Span metrics from one Collector, scraped through its Service | Exact with one replica | Scraping each Collector pod through service discovery |
| Single-node OpenSearch, two days of retention, no archive | Fits a workstation | A replicated cluster, index lifecycle management, retention set by cost and compliance |
| Sampling: all errors, all traces ≥ 1 s, 25% of the rest | A sensible default for low traffic | Baseline tuned to traffic volume and budget; per-route rates where needed |
| No alerting and no infrastructure metrics | The platform is examined through scenarios and validation | Alerts on the Collector's self-metrics (refused spans, early drops, export failures) and on the RED metrics; a cluster monitoring stack |

## Gateway

| Implemented now | Why it is simplified | Production |
| --- | --- | --- |
| One Kong replica; rate limits counted per pod (`local` policy) | Exact without Redis or a database | Several replicas with a shared counter (Redis) |
| Kong Gateway 3.9.3, the last open-source image, with a known tracing defect on 5xx answers | Keeps the platform fully open source | A supported gateway version: Kong's licensed image or another Gateway API implementation |
| Only loopback exposure (`127.0.0.1:8443`, `127.0.0.1:9443`) | A local environment must not be reachable from the network | A load balancer with a public certificate, DDoS protection and a web application firewall as required |

## Kubernetes and delivery

| Implemented now | Why it is simplified | Production |
| --- | --- | --- |
| kind on one machine: one control plane, two workers | Reproducible and free | A managed Kubernetes service with node pools and autoscaling |
| One replica for most components | Makes the cost of missing redundancy visible in experiments | At least two replicas, pod disruption budgets, anti-affinity across zones |
| No autoscaling | Load is generated deliberately, not organically | Horizontal autoscaling on request rate or latency |
| Images built locally and loaded into the nodes | No registry needed | A registry, signed images and an admission policy that verifies them |
| kind's local-path volumes inside the node containers | Simple | Storage classes with suitable performance, snapshots and backups |
| Deployments run by hand with `platformctl` | One operator, one workstation | GitOps (for example Argo CD or Flux) using the same charts and values |

## Operating experiments

| Implemented now | Production |
| --- | --- |
| Experiments started by hand, one at a time, under a Kubernetes Lease | Scheduled experiments with an owner, a limited blast radius and an abort switch |
| The restore journal is a local file on the operator's machine | The same idea, with the journal in shared storage so any operator can finish a restoration |
| Validation suites run on demand | The same suites as a gate after every deployment |
| Telemetry contracts verified per experiment | The same contracts applied continuously to samples of production traffic |

## Related documents

- [Known limitations](known-limitations.md) — the current behavior these recommendations address
- [Security](security.md) — the security controls and their local simplifications
- [Design decisions](design-decisions.md) — why the current design looks the way it does
- [Architecture](architecture.md) — the system as it is implemented
