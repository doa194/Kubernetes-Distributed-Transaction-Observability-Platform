# Versions and upgrades

Every version the platform depends on is pinned, so building it today and building it in a year
produce the same result. This document lists the pins, explains the compatibility constraints behind
them, and describes how to upgrade a component safely.

---

## Where versions are pinned

| What | Pinned in | How |
| --- | --- | --- |
| Tools, Kubernetes node image, Gateway API, Helm charts, container images | `deploy/versions.yaml` | One manifest; the automation passes its values to kind, Helm and Docker |
| .NET SDK | `global.json` | SDK 10.0.100 or a later 10.0 feature release |
| NuGet packages | `Directory.Packages.props` | Central versions; projects reference packages without a version |
| Python packages | `uv.lock` | Exact versions and hashes, installed by `uv run` |
| The five service images | — | Built locally and tagged with a hash of their source ([automation-cli.md](automation-cli.md#deployments-are-idempotent)) |

Because each version is written down exactly once, an upgrade is a single, reviewable change.

## The pins

| Area | Version | Notes |
| --- | --- | --- |
| kind | v0.33 | Checked by `preflight` |
| Helm | v4 | Checked by `preflight` |
| kubectl | 1.36 ± 1 minor | `preflight` fails if the client is more than one minor version away from the cluster |
| .NET SDK | 10 | Checked by `preflight` |
| Python | 3.12 or newer | Checked by `preflight` |
| Kubernetes node image | `kindest/node:v1.36.4@sha256:099e0493…` | Pinned **by digest**, so every cluster is identical |
| Gateway API | v1.3.0, standard channel | The definitions are stored in `deploy/crds/` |
| Kong chart | `kong/ingress` 0.24.0 | Kong Gateway and the Kong Ingress Controller |
| OpenSearch chart | `opensearch/opensearch` 3.8.0 | |
| Prometheus chart | `prometheus-community/prometheus` 29.30.0 | |
| Kong Gateway | 3.9.3 | |
| Kong Ingress Controller | 3.5.13 | |
| Keycloak | 26.7.4 | |
| Jaeger (Collector, Query) | 2.21.0 | Also the index cleaner |
| OpenSearch | 3.8.0 | |
| Prometheus | v3.14.0 | |
| .NET base images | `sdk:10.0` (build), `aspnet:10.0-noble-chiseled` (runtime) | The runtime image has no shell or package manager and runs as a non-root user |
| Probe pods | `busybox:1.37.0` | Used by the validation suites |

## Compatibility constraints

These constraints explain the current pins. Upgrading past them breaks the platform:

1. **Kubernetes 1.36 because of the Kong Ingress Controller.** Controller 3.5 supports Kubernetes
   1.29 to 1.36. kind v0.33 would create 1.37 clusters by default, so the node image is pinned to
   1.36.4 on purpose.
2. **Kong Gateway 3.9.3 is the last open-source image Kong published.** Moving past it means
   switching to Kong's commercially licensed image or to a different gateway.
3. **Jaeger bundles a fixed set of Collector components.** Jaeger 2.21 contains these processors
   only: `adaptive_sampling`, `attributes`, `batch`, `filter`, `memory_limiter` and `tail_sampling`;
   the connectors `forward` and `span_metrics`; and exporters such as `prometheus` and
   `jaeger_storage_exporter`. Common Collector processors like `resource`, `transform`, `redaction`
   and `k8sattributes` are **not** included. That is why Kubernetes metadata comes from the Downward
   API instead of `k8sattributes`, and redaction uses the `attributes` processor
   ([telemetry-pipeline.md](telemetry-pipeline.md)). To see the list for any Jaeger version:

   ```bash
   docker run --rm jaegertracing/jaeger:2.21.0 components
   ```

4. **The filter processor's condition syntax.** Health-span filtering uses `trace_conditions`, and
   `IsRootSpan()` is only available when the condition names `context: span` explicitly.
5. **Jaeger's query API.** The automation searches through API v3 with the parameters
   `query.serviceName`, `query.startTimeMin`, `query.startTimeMax`, `query.attributes` and
   `query.searchDepth` ([trace-storage.md](trace-storage.md#through-the-api)).
6. **`dotnet test` uses Microsoft.Testing.Platform.** `global.json` selects the new test platform.
   Options of the classic runner, such as `--nologo`, are passed on to the test application, which
   then runs no tests ("Zero tests ran").

### Feature gates

| Gate | Component | Why |
| --- | --- | --- |
| `connector.spanmetrics.excludeResourceMetrics` | Jaeger Collector | Keeps pod names and instance ids out of the span metrics, so a restart does not start new series |

Feature gates are experimental switches by definition; check the Collector's release notes when
upgrading Jaeger.

## Upgrading a component

1. **Change the pin** in `deploy/versions.yaml` — and the values file, if the new version renames a
   setting.
2. **For a Jaeger change, validate the Collector configuration first.** Jaeger ships a validator
   that checks the configuration without starting anything:

   ```bash
   mkdir -p .local/collector-check
   helm template observability deploy/charts/observability --namespace observability \
     --show-only templates/collector.yaml \
     | uv run python -c "import sys, yaml; print(next(d for d in yaml.safe_load_all(sys.stdin) if d and d['kind'] == 'ConfigMap')['data']['config.yaml'])" \
     > .local/collector-check/config.yaml
   docker run --rm -v "$PWD/.local/collector-check:/etc/jaeger:ro" jaegertracing/jaeger:2.21.0 \
     validate --config=/etc/jaeger/config.yaml --feature-gates=connector.spanmetrics.excludeResourceMetrics
   ```

   It exits with `0` for a valid configuration and names the problem otherwise, for example
   `references processor "transform" which is not configured`. In Git Bash on Windows, prefix the
   `docker run` line with `MSYS_NO_PATHCONV=1` so the container path is not rewritten.
3. **Deploy only the affected component:**

   ```bash
   uv run platformctl deploy --component observability
   ```

4. **Verify** with the suites that cover the component, then with a scenario that depends on it:

   ```bash
   uv run platformctl validate --suite observability --suite telemetry-pipeline
   uv run scenarioctl run normal-order
   ```

| Component changed | Suites to run | Scenarios worth running |
| --- | --- | --- |
| Kong or the Kong Ingress Controller | `edge`, `security` | `kong-rate-limit`, `oversized-request`, `normal-order` |
| Keycloak | `security` | `auth-denied`, `normal-order` |
| Jaeger | `observability`, `telemetry-pipeline`, `tracing` | `normal-order`, `trace-storage-outage` |
| OpenSearch | `observability` (and `observability-resilience`) | `trace-storage-outage` |
| Prometheus | `observability`, `telemetry-pipeline` | `traffic-spike` |
| .NET packages or base images | all default suites | the whole catalog |

## Upgrading Kubernetes

Change `kubernetes.nodeImage` — version **and** digest, taken from the kind release notes of the
kind version in use — then recreate the cluster:

```bash
uv run platformctl destroy
uv run platformctl bootstrap
```

Check the Kong Ingress Controller's supported Kubernetes range first. The `foundation` suite, which
runs at the end of every bootstrap, verifies that all three nodes run the pinned version.

## How drift is caught

| Drift | Caught by |
| --- | --- |
| A different kind, Helm, kubectl, .NET or Python version on the workstation | `platformctl preflight` (also the first step of `bootstrap`): a failure for missing or incompatible tools, a warning for untested versions |
| A cluster created with a different node image | The `foundation` suite's version check |
| A changed chart or image version | Explicit in `deploy/versions.yaml` and visible in `platformctl status` (chart versions) |
| A service built from different sources | A different content-hash tag, visible as `service.version` on every span |

## Related documents

- [Configuration reference](configuration-reference.md) — every setting and where it lives
- [Automation CLI](automation-cli.md) — `preflight`, `deploy` and image tagging
- [Validation suites](validation-suites.md) — what each suite verifies after an upgrade
- [Known limitations](known-limitations.md) — the constraints these pins imply
