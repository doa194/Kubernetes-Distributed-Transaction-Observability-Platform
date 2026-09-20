# Configuration reference

Every setting of the platform lives in a file under `deploy/` or in the services' code; nothing is
configured by hand after deployment, and no environment variables need to be set on the
workstation. This document lists where each setting lives, what it does, and how a change reaches
the running platform.

---

## Where settings live

```text
deploy/
├── versions.yaml                          every pinned version (tools, node image, charts, images)
├── kind/cluster.yaml                      the cluster: nodes and loopback port mappings
├── crds/                                  the vendored Gateway API definitions
├── charts/                                the project's own Helm charts, with their defaults
│   ├── platform-foundation/values.yaml    namespaces, Pod Security, network baseline, default limits
│   ├── gateway/values.yaml                Gateway, TLS secret, Kong's tracing plugin
│   ├── identity/values.yaml               Keycloak image, public URL, resources
│   ├── observability/values.yaml          Jaeger, sampling, span metrics, retention
│   └── transaction-services/values.yaml   the five services, edge policies, probes, shutdown
└── values/local/                          local overrides and settings for the upstream charts
    ├── transaction-services.yaml          turns on faults, telemetry and the public route; seed data
    ├── kong.yaml                          Kong Gateway and its ingress controller
    ├── opensearch.yaml                    OpenSearch
    └── prometheus.yaml                    Prometheus
```

The chart defaults are safe for production-like use — for example, fault injection is off. The files
in `deploy/values/local/` switch on what only the local environment should have.

## Applying a change

| Changed file | Command |
| --- | --- |
| `deploy/charts/platform-foundation/…` | `uv run platformctl deploy --component foundation` |
| `deploy/charts/gateway/…` | `uv run platformctl deploy --component gateway` |
| `deploy/values/local/kong.yaml` | `uv run platformctl deploy --component kong` |
| `deploy/charts/identity/…` | `uv run platformctl deploy --component identity` |
| `deploy/charts/observability/…`, `opensearch.yaml`, `prometheus.yaml` | `uv run platformctl deploy --component observability` |
| `deploy/charts/transaction-services/…`, `transaction-services.yaml`, code in `src/` | `uv run platformctl deploy --component services` |
| `deploy/versions.yaml` | `uv run platformctl deploy` (see [versions-and-upgrades.md](versions-and-upgrades.md)) |
| `deploy/kind/cluster.yaml` | Recreate the cluster: `uv run platformctl destroy`, then `uv run platformctl bootstrap` |

Only what changed restarts: pod templates carry checksums of their configuration (Collector and
Query configuration, seed data, Keycloak realm), and service images carry content hashes.

## Versions — `deploy/versions.yaml`

One manifest for every pinned version: tool versions checked by `preflight`, the Kubernetes node
image (with digest), the Gateway API version, chart versions and container images. The automation
passes these values to kind, Helm and Docker, so no version is written down twice
([versions-and-upgrades.md](versions-and-upgrades.md)).

## Cluster — `deploy/kind/cluster.yaml`

| Setting | Value |
| --- | --- |
| Name | `txplatform` (context `kind-txplatform`) |
| Nodes | 1 control plane, 2 workers |
| Port mappings | `127.0.0.1:8443 → 30443` (Kong), `127.0.0.1:9443 → 31443` (Keycloak) |
| Network plugin | kind's default, kindnet (it enforces NetworkPolicy) |

Port mappings can only be defined when a cluster is created, so changing them means recreating the
cluster.

## Namespaces — `platform-foundation/values.yaml`

```yaml
namespaces:
  - name: transaction-platform
    gatewayAccess: true                    # routes from here may attach to the shared Gateway
    podSecurity: { enforce: restricted, warn: restricted, audit: restricted }
    networkPolicy: { denyIngress: true, denyEgress: true }
  # gateway-system: denyEgress false (the ingress controller must reach the Kubernetes API)
  # observability:  like transaction-platform, without gateway access
defaultContainerResources:                 # LimitRange safety net; every workload declares its own
  requests: { cpu: 50m, memory: 64Mi }
  limits:   { cpu: 500m, memory: 256Mi }
```

## Gateway — `gateway/values.yaml` and `values/local/kong.yaml`

| Setting | Value | Meaning |
| --- | --- | --- |
| `gatewayClassName`, `gatewayName` | `kong`, `platform-gateway` | The Gateway API objects |
| `tlsSecretName` | `edge-tls` | The server certificate, created by the automation |
| `routeNamespaceLabel` | `txplatform.io/gateway-access: "true"` | Which namespaces may attach routes |
| `opentelemetry.tracesEndpoint` | Collector, OTLP/HTTP `:4318/v1/traces` | Where Kong sends its spans |
| `opentelemetry.serviceName` | `kong-gateway` | `service.name` of Kong's spans |
| `kong.yaml`: `gateway.proxy` | NodePort 30443, HTTPS only | No plaintext listener |
| `kong.yaml`: `gateway.manager.enabled` | `false` | Kong Manager is not deployed |
| `kong.yaml`: `tracing_instrumentations`, `tracing_sampling_rate` | `request,balancer`, `1.0` | Two spans per request, every request |
| `kong.yaml`: `headers` | `latency_tokens,X-Kong-Request-Id` | The Kong version is not advertised |

The **Order API's own edge policies** — route, rate limit, size limit, correlation id — belong to the
application and are set in `transaction-services/values.yaml` (next section).

## Application services — `transaction-services/values.yaml`

| Setting | Default | Meaning |
| --- | --- | --- |
| `ports.business`, `ports.management` | 8080, 8081 | The two ports of every service |
| `faults.enabled` | `false` (`true` in the local values) | Whether the fault endpoints exist |
| `telemetry.enabled`, `telemetry.otlpEndpoint` | `false` (`true` locally), Collector `:4317` | Whether and where spans are exported |
| `auth.issuer` | `https://localhost:9443/realms/transaction-platform` | Must equal the `iss` claim Keycloak writes |
| `auth.audience` | `order-api` | Required `aud` claim |
| `auth.metadataAddress` | Keycloak's internal discovery URL | Where signing keys are fetched |
| `edge.enabled` | `false` (`true` locally) | Whether the HTTPRoute and edge plugins are created |
| `edge.rateLimit.perSecond`, `perMinute` | 30, 1 500 | Kong rate limit per client IP |
| `edge.maxRequestKilobytes` | 16 | Kong request size limit |
| `edge.upstreamRetries`, `upstreamTimeoutMs` | 0, 15 000 | Kong never retries; waits longer than the 12-second transaction budget |
| `forwardedHeaders.trustedNetwork` | `10.244.0.0/16` | Whose `X-Forwarded-*` headers OrderService believes |
| `terminationGracePeriodSeconds`, `preStopSleepSeconds` | 30, 5 | Graceful shutdown |
| `services.<name>.replicas` | 1 (FraudService 2) | PaymentService must stay at 1 |
| `services.<name>.resources` | OrderService 100m/128Mi → 1 CPU/256Mi; others 50m/96Mi → 500m/192Mi | Requests and limits |
| `services.<name>.spreadAcrossNodes` | FraudService only | Spread replicas over the workers |
| `services.<name>.env` | empty | Extra environment variables for one service (see below) |
| `seed.<name>` | empty (filled locally) | Data mounted as `/etc/txplatform/seed.json` |

`services.<name>.tag` is set by the automation from each image's content hash and should not be
edited.

### Seed data

The local values file provides the catalog and the stock:

```yaml
seed:
  order-service:
    Catalog:
      Currency: EUR
      Items:
        - { Sku: SKU-1001, UnitPrice: 12.50 }
        - { Sku: SKU-1002, UnitPrice: 49.90 }
        - { Sku: SKU-2001, UnitPrice: 950.00 }
        - { Sku: SKU-9000, UnitPrice: 5.00 }
  inventory-service:
    Stock:
      Items:
        - { Sku: SKU-1001, Available: 1000000 }
        # … SKU-1002 and SKU-2001 likewise
        - { Sku: SKU-9000, Available: 0 }   # never in stock: a deterministic "insufficient stock"
```

The seed file is read at start-up like any other configuration source, and a checksum of it on the
pod template restarts exactly the affected service when it changes.

### Service options

Each service reads settings with built-in defaults. The chart sets the ones that depend on the
environment; the others can be overridden through `services.<name>.env`, using .NET's convention of
`__` between section and key:

| Service | Option | Default | Meaning |
| --- | --- | --- | --- |
| OrderService | `Transaction__Budget` | `00:00:12` | Time budget of one order transaction |
| FraudService | `Risk__MaxOrderAmount` | `5000` | Orders above this total are rejected |
| FraudService | `Risk__MaxItemCount` | `60` | Orders with more items are rejected |
| all | `Faults__MaxTtlSeconds` | `3600` | Longest lifetime a fault rule may request |

For example, to raise the fraud limit locally:

```yaml
# deploy/values/local/transaction-services.yaml
services:
  fraud-service:
    env:
      Risk__MaxOrderAmount: "10000"
```

Scenarios and checks assume the default business rules (for example, `fraud-rejection` relies on
the EUR 5 000 limit), so the scenario catalog expects the defaults.

### Environment set inside the pods

| Variable | Source | Purpose |
| --- | --- | --- |
| `POD_NAME`, `POD_NAMESPACE`, `POD_UID`, `NODE_NAME` | Downward API | Pod facts for trace resource attributes |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL` | `telemetry` values | Export spans over OTLP/gRPC to the Collector |
| `OTEL_BSP_SCHEDULE_DELAY` | fixed `1000` | Export finished spans every second |
| `OTEL_RESOURCE_ATTRIBUTES` | the pod facts above | `k8s.*`, `service.namespace`, `service.instance.id`, `deployment.environment.name` |
| `SERVICE_VERSION` | the image tag | `service.version` of every span |
| `Ports__Business`, `Ports__Management` | `ports` values | Listening ports |
| `Faults__Enabled` | `faults.enabled` | Fault endpoints on or off |
| `Auth__*`, `ForwardedHeaders__TrustedNetwork` | `auth`, `forwardedHeaders` (OrderService only) | Token validation and proxy trust |
| `Dependencies__{Inventory,Fraud,Payment,Shipping}BaseUrl` | Service names and the business port (OrderService only) | Where the dependencies are |

## Identity — `identity/values.yaml` and the realm file

| Setting | Value |
| --- | --- |
| `publicUrl` | `https://localhost:9443` — becomes the issuer of every token |
| `nodePort` | 31443 |
| `secrets.admin`, `secrets.clients`, `secrets.tls` | Names of the secrets created by the automation; the chart never contains secret values |
| `resources` | 250m/768Mi → 1.5 CPU/1Gi |

The realm — clients, roles, token lifetime, mappers — is defined in
`deploy/charts/identity/files/transaction-platform-realm.json` and imported when Keycloak starts
([identity-and-tokens.md](identity-and-tokens.md)).

## Observability — `observability/values.yaml`

```yaml
openSearchUrl: http://opensearch-traces.observability.svc.cluster.local:9200
prometheusUrl: http://prometheus-server.observability.svc.cluster.local:9090
indexPrefix: txplatform
retentionDays: 2
collector:
  resources: { requests: { cpu: 200m, memory: 384Mi }, limits: { cpu: "1", memory: 768Mi } }
  sampling:
    decisionWait: 20s
    maxTracesInMemory: 20000
    expectedNewTracesPerSecond: 100
    decisionCacheSize: 50000
    slowThresholdMs: 1000
    baselinePercentage: 25
  spanMetrics:
    cardinalityLimit: 2000
query:
  resources: { requests: { cpu: 50m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
otlpProducers:                             # who may send spans (enforced by network policies)
  grpc: { namespace: transaction-platform, identities: [order-service, inventory-service, fraud-service, payment-service, shipping-service] }
  http: { namespace: gateway-system, identities: [kong-proxy] }
```

The Collector's and Query's full configurations are built from these values in
`templates/_configs.tpl` ([telemetry-pipeline.md](telemetry-pipeline.md)). Sampling settings are
explained in [sampling.md](sampling.md).

## Upstream charts — `values/local/`

| File | Notable settings |
| --- | --- |
| `kong.yaml` | See [Gateway](#gateway--gatewayvaluesyaml-and-valueslocalkongyaml) above; network identity labels for both pods |
| `opensearch.yaml` | Single node, 768 MiB heap in a 1.5 GiB limit, 5 GiB volume, security plugin off, `node.store.allow_mmap: false`, root helper container disabled, readiness on the cluster-health endpoint |
| `prometheus.yaml` | Server only (no Alertmanager, exporters or Pushgateway), no RBAC, 2-day retention, 15-second scrapes, four fixed scrape jobs |

## Deliberately not configurable

Timeouts, retry counts and circuit-breaker thresholds per dependency are defined in code
(`src/OrderService/Infrastructure/Dependencies/DependencyPolicies.cs`), as are the payment and
shipping test rules. They are part of the designed behavior: unit tests assert their relationships
(for example, that every stage budget leaves room for all attempts), and the scenarios depend on
their values. Changing them is a code change with matching test changes, not a deployment setting
([resilience.md](resilience.md)).

## Related documents

- [Versions and upgrades](versions-and-upgrades.md) — changing pinned versions
- [Operations](operations.md) — applying changes to a running platform
- [Kubernetes platform](kubernetes-platform.md) — what the charts create
- [Security](security.md) — which settings are local simplifications
