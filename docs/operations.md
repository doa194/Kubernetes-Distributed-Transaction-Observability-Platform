# Operations

Everyday tasks on a running platform: checking its state, finding a request, reaching the internal
endpoints, applying a change, cleaning up after an experiment, and looking after trace storage and
certificates. Each task is a short recipe; the commands run from the repository root.

---

## Check the state of everything

```bash
uv run platformctl status
```

```text
==> Nodes
    txplatform-control-plane     Ready     v1.36.4
    txplatform-worker            Ready     v1.36.4
    txplatform-worker2           Ready     v1.36.4
==> Namespaces
    gateway-system         pod-security=restricted
    ...
==> Workloads
    transaction-platform   deploy/fraud-service                  2/2 ready
    ...
==> Experiments
    experiment lock: free
    every recorded run was restored
==> Certificates
    server certificate valid for 89 more days (CA: …/.local/pki/ca.crt)
```

It shows the nodes and their Kubernetes version, the Pod Security level of each namespace, the Helm
releases, the readiness of every workload, whether an experiment is running or a run still needs
restoring, and how long the local server certificate remains valid.

For a deeper check, run the validation suites (about five minutes):

```bash
uv run platformctl validate
```

## Open the UIs

```bash
uv run platformctl ui jaeger        # traces:  http://127.0.0.1:16686
uv run platformctl ui prometheus    # metrics: http://127.0.0.1:9090
```

Both are cluster-internal services, reached through a port-forward that stays open until Ctrl+C.

## Find one request

Every Order API response carries the ids needed to find its telemetry:

| Header | Use |
| --- | --- |
| `traceresponse: 00-<trace id>-…` | Open `http://127.0.0.1:16686/trace/<trace id>` |
| `X-Correlation-ID` | In Jaeger, search service `order-service` with the tag `correlation.id=<id>` |

To see everything one experiment did, search for the tag `scenario.run_id=<run id>`; the run id is
printed by `scenarioctl` and stored in `.runs/`. More search patterns are listed in
[trace-storage.md](trace-storage.md#reading-traces).

## Reach a service's management port

Health checks, fault rules, the circuit-breaker reset and the payment ledger live on each service's
management port (8081), which no Service or pod can reach. A port-forward reaches it directly:

```bash
kubectl -n transaction-platform port-forward deploy/payment-service 8081:8081
```

In a second terminal:

```bash
curl -s http://127.0.0.1:8081/health/ready                          # Healthy
curl -s http://127.0.0.1:8081/internal/faults                       # active fault rules: []
curl -s http://127.0.0.1:8081/internal/payments/<order id>          # {"orderId":"…","authorizations":1,"voided":0}
```

| Endpoint | Service | Purpose |
| --- | --- | --- |
| `GET /health/live`, `GET /health/ready` | all | Liveness and readiness, as used by Kubernetes |
| `GET`, `PUT`, `DELETE /internal/faults` | the four dependencies | Fault rules ([fault-injection.md](fault-injection.md)) |
| `POST /internal/resilience/reset` | OrderService | Close all circuit breakers ([resilience.md](resilience.md)) |
| `GET /internal/payments/{orderId}` | PaymentService | How often an order was authorized and voided |

A port-forward to a Deployment picks one of its pods; for FraudService, which has two, forward to a
specific pod instead (`kubectl -n transaction-platform get pods`, then `port-forward pod/<name>`).
Port-forwarding travels through the Kubernetes API rather than the pod network, which is why it
works although every network policy denies access to port 8081
([network-policies.md](network-policies.md#port-forwarding-is-not-affected)).

## Apply a change

Edit the code or configuration, then redeploy the affected component:

| What changed | Command | What restarts |
| --- | --- | --- |
| .NET code in `src/` | `uv run platformctl deploy --component services` | Only services whose image content changed |
| Collector, Query, OpenSearch or Prometheus settings | `uv run platformctl deploy --component observability` | Only components whose configuration changed |
| Kong routes, limits or plugins of the Order API | `uv run platformctl deploy --component services` | Nothing — Kong's controller applies the change |
| Keycloak realm | `uv run platformctl deploy --component identity` | Keycloak (its pod template carries a checksum of the realm file) |
| Anything, or not sure | `uv run platformctl deploy` | Only what changed |

Where each setting lives is listed in [configuration-reference.md](configuration-reference.md).
After a change, `uv run platformctl validate` confirms that the platform still behaves as designed.

## Clean up after an experiment

A run normally restores the platform itself. After a crash, a killed terminal or a machine sleep in
the middle of a run:

```bash
uv run scenarioctl reset            # restore interrupted runs, remove fault rules, close breakers
uv run scenarioctl reset --force    # also release an experiment lock left by a process that is gone
```

`reset` is safe at any time and changes nothing on a clean platform. Even without it, fault rules
expire on their own and an abandoned lock frees itself after 90 seconds
([experiment-safety.md](experiment-safety.md)).

## Re-check a recorded experiment

```bash
uv run scenarioctl report                    # print the latest run
uv run scenarioctl verify <run id>           # repeat its telemetry checks, without new traffic
```

`verify` is useful after changing a telemetry contract: it re-reads the traces and metrics of the
recorded run, as long as they are still within the two-day retention.

## Trace storage

Indices are created per day and deleted by a nightly job after two days
([trace-storage.md](trace-storage.md)). To look at them:

```bash
kubectl -n observability port-forward svc/opensearch-traces 9200:9200
```

```bash
curl -s 'http://127.0.0.1:9200/_cat/indices/txplatform-*?v'
```

To run the retention job immediately instead of waiting for the night:

```bash
kubectl -n observability create job --from=cronjob/jaeger-index-cleaner cleanup-now
kubectl -n observability logs -f job/cleanup-now
kubectl -n observability delete job cleanup-now
```

## Certificates

The server certificate for `localhost` is valid for 90 days, the local CA for one year;
`platformctl status` shows the remaining days. Any `platformctl deploy` that includes the
`certificates` component reissues the server certificate once fewer than 14 days remain, and updates
the TLS secrets. Kong picks up the new secret through its controller; Keycloak reads its certificate
files when it starts, so restart it after a renewal:

```bash
uv run platformctl deploy --component certificates
kubectl -n transaction-platform rollout restart deployment/keycloak
```

Clients trust the certificate through the CA file `.local/pki/ca.crt` (for example
`curl --cacert .local/pki/ca.crt`).

## After Docker or the machine restarts

The kind nodes are Docker containers that restart with Docker, and every pod comes back by itself.
Data the services hold only in memory is gone — the orders, the payment ledger and the stock levels
return to their seeded state. Traces and metrics are kept on their volumes. Wait until everything
is ready, then carry on:

```bash
uv run platformctl status
```

## Remove the platform

```bash
uv run platformctl destroy           # delete the cluster
uv run platformctl destroy --purge   # also delete .local (the local CA and certificates)
docker image rm $(docker image ls 'txplatform/*' -q)   # optional: the built service images
```

Run records in `.runs/` are kept until you delete the folder.

## Related documents

- [Automation CLI](automation-cli.md) — every command and option
- [Troubleshooting](troubleshooting.md) — when something does not work
- [Configuration reference](configuration-reference.md) — where each setting lives
- [Walkthrough](walkthrough.md) — a guided first tour of a running platform
