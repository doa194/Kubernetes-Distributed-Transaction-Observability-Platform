# Validation suites

Validation suites answer one question: **does the deployed platform actually behave as designed?**
They run against the real cluster and never inspect configuration files. Each check asks the platform
something — admit this privileged pod, connect to this port, store this trace — and compares the
answer with the design. This document lists every suite and check and explains how to run them.

---

## Running the suites

```bash
uv run platformctl validate                                    # the six default suites: 42 checks, about 5 minutes
uv run platformctl validate --suite tracing                    # one suite
uv run platformctl validate --suite edge --suite security      # several suites
uv run platformctl validate --suite observability-resilience   # a disruptive suite, only on request
```

| Group | Suites | Checks | When |
| --- | --- | --- | --- |
| **Smoke** | foundation, security, tracing, edge, observability | 32 | Automatically at the end of `platformctl bootstrap` |
| **Default** | the smoke suites + telemetry-pipeline | 42 | `platformctl validate` without options |
| **Disruptive** | observability-resilience, recovery | 5 | Only when named explicitly — they restart, stop or overload components |

Validation takes the experiment lock, so it never runs at the same time as a scenario or a
deployment ([experiment-safety.md](experiment-safety.md)). The output lists each check with its
duration and evidence:

```text
==> Suite 'telemetry-pipeline': tail sampling, unsampled span metrics, safe filtering, sanitation, cardinality, SPM, self-metrics
[ OK ] error, slow and debug traces are always kept (42.5s): 5/5 error, 5/5 slow, 5/5 debug traces stored
[ OK ] normal traces are kept at the baseline rate (7.9s): 98 of 400 kept (expected about 100)
[ OK ] span metrics count every span, including traces that sampling dropped (0.3s): 400 calls counted for 400 spans, most of them not stored
...
==> 42 of 42 checks passed
```

The command exits with `0` only if every check passed. Checks that wait for the platform (a pod to
become ready, a trace to be stored, a policy to take effect) poll against a deadline instead of
sleeping for a fixed time, and every suite removes the temporary objects it created.

## Test tools the suites use

| Tool | What it does |
| --- | --- |
| **Probe pods** | Short-lived pods with a chosen network identity in a chosen namespace. They try TCP connections or HTTP requests from *inside* the cluster and report what worked. They comply with the restricted Pod Security level and are deleted afterwards |
| **Server-side dry runs** | Ask the Kubernetes API to admit an object without creating it — used to prove that Pod Security rejects a privileged pod |
| **Synthetic spans** | Spans with exactly chosen status, duration and attributes, sent straight to the Collector over OTLP from a fictitious service (`pipeline-probe`) |
| **Real orders** | Placed through Kong or directly at OrderService through a port-forward, usually with the debug flag so their traces are kept |

## foundation — 7 checks

*Is the cluster itself what the platform assumes?*

| Check | How it is proven |
| --- | --- |
| Nodes ready on the pinned Kubernetes version | 3 nodes, all `Ready`, all running the version from `deploy/versions.yaml` |
| Namespaces carry Pod Security levels | Each platform namespace enforces `restricted` |
| Platform namespaces reject a privileged pod | A privileged pod is submitted as a dry run to each namespace and must be refused |
| A persistent volume claim binds and stores data | A claim is created, a pod writes to it, the claim must be `Bound` |
| NetworkPolicy is enforced for real traffic | A client reaches a test server; after a deny policy is applied, it must no longer reach it |
| Platform containers declare their own resources, none was killed for memory | Every container in every workload template declares requests and limits, and no container was OOM-killed |
| Public host ports listen on loopback only | The kind node's port mappings are exactly the two expected ones, bound to `127.0.0.1` |

## security — 7 checks

*Are identities, permissions, network boundaries and secrets as designed?*

| Check | How it is proven |
| --- | --- |
| Keycloak serves verified TLS with the public issuer | The discovery document is fetched over HTTPS with the local CA and names the public issuer |
| In-cluster discovery uses the public issuer and the internal key endpoint | A probe pod with OrderService's identity fetches the internal discovery document |
| Client tokens carry the `order-api` audience and exact permissions | Each client's token is decoded: correct audience, issuer, and exactly its permissions |
| OrderService enforces authentication and permissions | No token → 401; read-only token creating an order → 403; read-only token reading → 404 (allowed, order unknown); full token → 201 |
| Network policies enforce the connection matrix | 13 connections probed from inside the cluster, each with an expected result ([network-policies.md](network-policies.md)) |
| Only the gateway and the identity provider are reachable from outside | The only NodePort or LoadBalancer ports in the whole cluster are Kong's 30443 and Keycloak's 31443 |
| Secret values never appear in ConfigMaps or Helm values | The three generated secret values are searched for in every ConfigMap and every release's values |

## tracing — 3 checks

*Does a real request produce a complete, Kubernetes-aware trace?*

| Check | How it is proven |
| --- | --- |
| An order trace reaches Jaeger with all five services | An order is placed with the debug flag; its trace must contain all five .NET services |
| Spans carry Kubernetes metadata matching the live pods | Every .NET span's namespace, pod, node and deployment must match a pod that exists |
| Health probes produce no operations in Jaeger | No .NET service lists an operation for a health or management path (`/health`, `/internal`) |

## edge — 9 checks

*Does the gateway route, protect and trace as designed?*

| Check | How it is proven |
| --- | --- |
| Gateway and HTTPRoute are accepted and programmed | The Gateway API status conditions are read |
| HTTPS uses TLS 1.2+ with the local CA certificate | A TLS connection to port 8443; no plaintext listener exists |
| Only `/orders` is routed, and it reaches OrderService's authentication | `/orders` answers 401 from OrderService; other paths answer 404 from Kong |
| Routes from namespaces without gateway access are rejected | A route created in a namespace without the access label must be rejected, and its path must not be served |
| Correlation ids are generated when absent and preserved when valid | Requests with and without `X-Correlation-ID` |
| `Location` uses the public HTTPS address; spoofed forwarding headers are ignored | An order through Kong gets `https://localhost:8443/orders/…`; a direct request with a forged `X-Forwarded-Proto: https` still gets an `http://` address |
| Kong never retries order requests upstream | Kong's admin API reports 0 retries for the route's upstream service |
| Kong starts every trace and ignores client trace context | A request with a client `traceparent` and `baggage`: the trace id must be new, the only root must be Kong's span, OrderService's span must descend from it and carry the Kong request id, and the client's baggage must not reach any span |
| Gateway network policies limit Kong to its intended connections | Probe pods test Kong's proxy and admin API connections |

## observability — 6 checks

*Are traces stored, served and retained, and is the telemetry stack isolated?*

| Check | How it is proven |
| --- | --- |
| OpenSearch, Collector, Query and Prometheus are ready | Workload status, and OpenSearch cluster health `green` |
| A gateway order is stored in OpenSearch and served by Jaeger Query | The span count in today's index equals the span count Query serves |
| Prometheus scrapes Collector and Query telemetry | All scrape targets are `up` |
| The Collector accepts spans without refusals or export failures | Its self-metrics after the order above |
| The retention job deletes trace indices of its own prefix only | The real job runs once against a throwaway prefix ([trace-storage.md](trace-storage.md#retention)) |
| Only Collector, Query and the cleaner reach OpenSearch; Query and Prometheus stay private | 7 connections probed from inside the cluster |

## telemetry-pipeline — 10 checks

*Does the Collector sample, count, filter and redact correctly?* The suite sends synthetic spans with
exactly chosen properties, waits for the sampling decision, and inspects the result. The checks are
described in [telemetry-pipeline.md](telemetry-pipeline.md#what-is-verified) and
[sampling.md](sampling.md#how-the-policies-are-verified).

| Check |
| --- |
| Error, slow and debug traces are always kept |
| Normal traces are kept at the baseline rate |
| Span metrics count every span, including traces that sampling dropped |
| Late spans join traces that were already kept |
| Health spans are filtered without breaking traces |
| Sensitive attributes are removed and client addresses hashed |
| Kong's legacy HTTP status becomes the standard metric dimension |
| Span metrics carry no high-cardinality identifiers |
| Jaeger's Monitor API returns RED metrics for real services |
| Collector self-metrics show healthy sampling and ingestion |

## observability-resilience — 3 checks (disruptive)

*Do the telemetry components fail independently?* Each check disturbs one component and always
restores it.

| Check | How it is proven |
| --- | --- |
| Stored traces survive an OpenSearch pod restart | A stored trace is served complete after the pod is deleted and replaced |
| Query serves stored traces while the Collector is down | The Collector is scaled to zero; a stored trace is still served, and a new order still succeeds |
| The Collector keeps ingesting while Query is down | Query is scaled to zero; a trace produced meanwhile is available once Query returns |

## recovery — 2 checks (disruptive)

*Is the platform stable when it is redeployed or overloaded?*

| Check | How it is proven |
| --- | --- |
| Redeploying every component replaces or restarts no healthy pod | All eight components are deployed again; every pod keeps its identity and restart count |
| An overflow of the Collector's sampling buffer is bounded, visible and survived | 25 000 traces are sent to a 20 000-trace buffer; early drops are counted, the Collector keeps running, and a later trace is stored ([sampling.md](sampling.md#overload-behavior)) |

## What validation does not cover

Validation proves the platform's **properties**. Behavior under specific failures — a slow payment,
a lost reply, a deleted pod — is covered by the scenarios ([scenario-catalog.md](scenario-catalog.md)),
and the logic inside the services and the automation by unit, component and integration tests
([testing-strategy.md](testing-strategy.md)).

## Related documents

- [Testing strategy](testing-strategy.md) — where validation fits among the test layers
- [Network policies](network-policies.md) — the connections the probes test
- [Security](security.md) — the controls the security suite verifies
- [Automation CLI](automation-cli.md) — the `validate` command
