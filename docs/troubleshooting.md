# Troubleshooting

Problems you may meet when running the platform, each with its cause and the quickest fix. Start
with the two commands below; most problems announce themselves there.

---

## First steps

```bash
uv run platformctl status      # nodes, releases, workloads, experiment lock, certificate
uv run platformctl validate    # 42 checks; the first failing check usually names the cause
```

| Symptom area | Go to |
| --- | --- |
| `preflight`, `bootstrap` or `deploy` fails | [Setting up and deploying](#setting-up-and-deploying) |
| A request is rejected or fails | [Requests](#requests) |
| A trace or metric is missing or looks wrong | [Traces and metrics](#traces-and-metrics) |
| A scenario refuses to start or fails | [Experiments](#experiments) |
| A test run fails | [Tests](#tests) |
| Windows-specific errors | [Windows](#windows) |

## Setting up and deploying

### `preflight` fails on Docker CPUs or memory

**Cause.** The platform needs at least 4 CPUs and 8 GB of memory inside Docker; OpenSearch and
Keycloak alone need 2.5 GB. **Fix.** Raise the limits (Docker Desktop: *Settings → Resources*) and run
`uv run platformctl preflight` again.

### `preflight` reports port 8443 or 9443 in use

**Cause.** Another program listens on the port. (A port bound by this project's own cluster is
reported as fine.) **Fix.** Stop the other program; the ports are fixed because they are part of the
cluster definition.

### `uv: command not found`

**Cause.** `uv` is not on your `PATH`. **Fix.** Install it, or — if it lives in the project's
`.tools/bin` — add that folder to `PATH` ([setup-guide.md](setup-guide.md#software)).

### `kube context 'kind-txplatform' does not exist; run platformctl bootstrap first`

**Cause.** The cluster has not been created, or was deleted. **Fix.** `uv run platformctl bootstrap`.

### A deployment stops with "release … is pending-upgrade since …"

**Cause.** A Helm operation was interrupted, and Helm refuses further changes to a release in that
state. **Fix.** If another deployment might still be running, let it finish. Otherwise wait: once a
release has been pending for 20 minutes — longer than any Helm timeout the automation uses — the next
`platformctl deploy` rolls it back (or removes an unfinished first install) and continues on its own,
reporting `… was left pending-upgrade by an interrupted deployment: rollback done`.

### Docker or the machine restarted in the middle of a deployment

**Cause.** The deployment process lost its connection to the cluster; the release may be left pending
and the experiment lock left held. **Fix.** Nothing special: a held lock expires after 90 seconds, and
a pending release is recovered as described above. Run the same `platformctl deploy` again.

### Pods stay `Pending`

**Cause.** Usually not enough memory in Docker. **Fix.** `kubectl describe pod <pod> -n <namespace>`
shows the reason under *Events*. The cluster has no metrics server, so `kubectl top` does not work —
compare Docker's memory limit with the [resource footprint](kubernetes-platform.md#resource-footprint)
instead.

### A pod restarts repeatedly

**Cause.** Its liveness probe fails or it crashes at start. **Fix.** Read the logs of the previous
attempt: `kubectl -n <namespace> logs <pod> --previous`, and the pod's events with
`kubectl -n <namespace> describe pod <pod>`. Note that OrderService does not restart while Keycloak is
unreachable: it stays running but reports itself unready until it can fetch the signing keys.

## Requests

### Every request answers 401

**Cause.** The token is missing, expired (tokens live 5 minutes), or was issued for a different
issuer or audience. **Fix.** Fetch a fresh token from `https://localhost:9443` — the issuer must be
exactly `https://localhost:9443/realms/transaction-platform` ([identity-and-tokens.md](identity-and-tokens.md)).
If Keycloak's pod was replaced, tokens issued before are invalid; OrderService picks up the new
signing keys within 30 seconds.

### 403 with a valid token

**Cause.** The client lacks the permission: creating an order needs `orders.write`. The
`order-reader` client has only `orders.read` on purpose. **Fix.** Use the `scenario-runner` client.

### 429 or 413 before reaching the service

**Cause.** The gateway's limits: 30 requests per second (and 1 500 per minute) per client IP, and
16 KB per request body. **Fix.** Slow down, or shrink the request ([gateway.md](gateway.md)).

### 404 from the gateway

**Cause.** Only paths starting with `/orders` are routed. **Fix.** Check the path; Keycloak is reached
on port 9443, not through the gateway.

### Orders fail with 502 or 504 although every pod is running

**Cause.** Often a fault rule left from manual experimenting (scenario runs remove their own).
**Fix.** `uv run scenarioctl reset` removes every fault rule and closes every circuit breaker. If
failures continue, open the trace: `failure.stage` and `failure.kind` on `order.transaction` name the
failing dependency ([transaction-flow.md](transaction-flow.md)).

### Orders fail fast with 503 for a while after a dependency failed

**Cause.** The dependency's circuit breaker is open: after half of at least ten calls failed within
30 seconds, calls are refused for 5 seconds at a time. **Fix.** It closes by itself once a trial call
succeeds; `uv run scenarioctl reset` closes it immediately ([resilience.md](resilience.md)).

## Traces and metrics

### A trace is missing in Jaeger

**Cause.** Either it was not sampled (ordinary traces are kept at 25%), or the decision has not been
made yet: nothing is stored until 20 seconds after a trace's first span, plus export and indexing.
**Fix.** Wait about 35 seconds. To keep a specific request's trace, send `X-Debug-Trace: true` with a
token that has the `traces.debug` permission ([sampling.md](sampling.md)).

### A failed order's trace shows a gap below the gateway

**Cause.** A defect in Kong 3.9.3: on a 5xx answer it exports a new balancer span instead of the one it
announced to OrderService. **Fix.** None needed; the trace checks recognize exactly this pattern
([gateway.md](gateway.md#known-defect-a-missing-parent-span-on-5xx-responses)).

### No traces at all

**Cause.** The Collector is not running or refuses data. **Fix.**

```bash
kubectl -n observability get pods
kubectl -n observability logs deploy/jaeger-collector --tail=50
```

In Prometheus, `otelcol_receiver_accepted_spans` should rise with traffic and
`otelcol_receiver_refused_spans` should stay flat. Spans sent while the Collector was down are lost;
the services do not buffer them.

### Jaeger's Monitor tab is empty

**Cause.** A rate needs at least two scrapes, so a new service or operation appears about half a
minute after its first request. **Fix.** Wait; if it stays empty, check that the `span-metrics`
target is `up` in Prometheus (`uv run platformctl ui prometheus`, then *Status → Targets*).

## Experiments

### "… holds the experiment lock"

**Cause.** Another scenario, validation or deployment is running. **Fix.** Wait for it —
`platformctl status` shows the holder. A lock whose holder died expires after 90 seconds;
`uv run scenarioctl reset --force` releases it immediately.

### Pre-flight fails: "fault rules left from an earlier run"

**Cause.** A fault rule is still active in a pod. **Fix.** `uv run scenarioctl reset`.

### Pre-flight fails: "… is not available"

**Cause.** A component is still starting, or an interrupted experiment left it scaled down. **Fix.**
`uv run scenarioctl reset` restores interrupted runs; then check `uv run platformctl status`.

### A scenario fails only its telemetry checks

**Cause.** Either the telemetry really is wrong — which is what the check exists for — or the
checks ran against incomplete data. **Fix.** Read the failing check's detail, then repeat the
verification without new traffic:

```bash
uv run scenarioctl report        # the latest run, with every check's detail
uv run scenarioctl verify        # repeat its telemetry checks
```

`.runs/<run id>/record.json` lists every request with its correlation id, Kong request id, trace id
and order id, so each failure can be followed to a concrete trace.

## Tests

### `dotnet test` reports "Zero tests ran" (exit code 5)

**Cause.** The projects run on Microsoft.Testing.Platform (selected in `global.json`). Options meant
for the classic test runner, such as `--nologo`, are passed on to the test application, which then
runs no tests. **Fix.** Run plain `dotnet test`.

### The Keycloak realm contract test fails to start

**Cause.** It starts Keycloak in a container and needs Docker. **Fix.** Start Docker, then run
`dotnet test` again.

### `pytest` runs no end-to-end tests

**Cause.** They are excluded by default because they need the running platform. **Fix.**
`uv run pytest -m e2e`.

## Windows

### `curl: (60) schannel: the revocation status is unknown`

**Cause.** curl on Windows (both Windows' own and Git Bash's) asks Windows to check whether the
certificate was revoked, and a local certificate authority cannot answer that. **Fix.** Add
`--ssl-revoke-best-effort`:

```bash
curl --cacert .local/pki/ca.crt --ssl-revoke-best-effort https://localhost:8443/orders/00000000-0000-0000-0000-000000000000
```

### `docker run -v "$PWD/…:/etc/…"` mounts the wrong folder in Git Bash

**Cause.** Git Bash rewrites arguments that look like Unix paths. **Fix.** Prefix the command with
`MSYS_NO_PATHCONV=1`.

### `kubectl` processes remain after port-forwards

**Cause.** Package managers such as Chocolatey install `kubectl` as a launcher that starts the real
program as a child process; stopping only the launcher leaves the child running. **Fix.** The
automation stops the whole process tree of every port-forward it starts. For port-forwards started by
hand, stop them with Ctrl+C in their terminal.

## Where the logs are

```bash
kubectl -n transaction-platform logs deploy/order-service --tail=100
kubectl -n gateway-system logs deploy/kong-gateway --tail=100
kubectl -n transaction-platform logs deploy/keycloak --tail=100
kubectl -n observability logs deploy/jaeger-collector --tail=100
```

The .NET services write JSON logs with the trace id and span id on every line written during a
request, so a log line leads to its trace and back ([tracing.md](tracing.md#logs-link-back-to-traces)).

## Related documents

- [Operations](operations.md) — everyday tasks
- [Setup guide](setup-guide.md) — prerequisites and the first bootstrap
- [Experiment safety](experiment-safety.md) — locks, journals and restoration
- [Known limitations](known-limitations.md) — behavior that is expected, not broken
