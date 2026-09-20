# Automation CLI

Two command-line tools operate the platform. **`platformctl`** creates, deploys, checks and removes
it; **`scenarioctl`** runs experiments against it. This document is the reference for both: every
command, its options, what it changes, and how it behaves when something goes wrong.

---

## How to call them

Both tools are Python entry points of the project. `uv run` starts them in the project's locked
environment, creating it from `uv.lock` on first use:

```bash
uv run platformctl --help
uv run scenarioctl --help
```

All commands run from the repository root and work the same way in bash and PowerShell.

| Tool | Purpose | Changes the platform? |
| --- | --- | --- |
| `platformctl` | Lifecycle: preflight, bootstrap, deploy, validate, status, UIs, destroy | Yes — creates, updates and deletes it |
| `scenarioctl` | Experiments: run, verify, report, reset | Temporarily — every change is journaled and undone |

Keeping the two apart makes the destructive commands obvious, and it keeps the lifecycle usable
without any experiment machinery. Both share one library (`automation/src/txplatform`), described in
[codebase-guide.md](codebase-guide.md).

## `platformctl`

| Command | What it does |
| --- | --- |
| `preflight` | Checks Docker resources, tool versions, free disk space and the two loopback ports |
| `bootstrap [--skip-validation]` | Runs preflight, creates the cluster, deploys every component, then runs the smoke validation (32 checks) |
| `deploy [--component NAME]…` | Deploys the named components (repeat the option for several); all components by default |
| `validate [--suite NAME]…` | Runs validation suites; by default every non-disruptive suite (42 checks) |
| `status` | Shows nodes, namespaces, Helm releases, workload readiness, the experiment lock, and the certificate's remaining validity |
| `ui {jaeger,prometheus} [--port N]` | Opens a cluster-internal UI on `127.0.0.1` through a port-forward, until Ctrl+C |
| `destroy [--purge]` | Deletes the kind cluster; `--purge` also deletes `.local` (the local CA and certificates) |

### Components

`deploy` and `bootstrap` work in terms of eight components, always applied in this order:

| Component | Deploys |
| --- | --- |
| `foundation` | Namespaces with Pod Security levels, default resource limits, deny-all and DNS network policies |
| `certificates` | The local CA and server certificate in `.local/pki`, and the TLS secrets `edge-tls` and `keycloak-tls` |
| `crds` | The Gateway API and Kong custom resource definitions |
| `gateway` | GatewayClass, HTTPS Gateway, Kong's global tracing plugin and the gateway network policies |
| `kong` | The Kong Ingress Controller and the DB-less Kong Gateway |
| `identity` | Generated secrets, and Keycloak with the `transaction-platform` realm |
| `observability` | OpenSearch, then Jaeger Collector and Query with the index cleaner, then Prometheus |
| `services` | Builds the five .NET images if needed, loads them into kind, and deploys them |

The order matters in one non-obvious place: `gateway` comes before `kong`, because the gateway chart
contains the network policy that lets Kong's controller reach Kong's admin API. Without it, the
controller could not configure Kong. Details of every component are in
[kubernetes-platform.md](kubernetes-platform.md).

```bash
uv run platformctl deploy --component services                         # after changing .NET code
uv run platformctl deploy --component observability                    # after changing the Collector or storage
uv run platformctl deploy --component gateway --component kong         # several components
```

### Validation suites

```bash
uv run platformctl validate                                   # the six default suites
uv run platformctl validate --suite tracing                   # one suite
uv run platformctl validate --suite recovery                  # a disruptive suite, only on request
```

The suites and their checks are described in [validation-suites.md](validation-suites.md).

### Opening the UIs

```bash
uv run platformctl ui jaeger                  # http://127.0.0.1:16686/search
uv run platformctl ui prometheus              # http://127.0.0.1:9090/targets
uv run platformctl ui jaeger --port 26686     # if the default port is taken
```

## `scenarioctl`

| Command | What it does |
| --- | --- |
| `list` | Lists all scenarios with category and title |
| `show ID` | Validates a scenario file and prints the parsed definition as JSON |
| `run ID [--repeat N] [--skip-telemetry]` | Runs the experiment and verifies it; `--skip-telemetry` checks only the application results |
| `verify [RUN_ID]` | Repeats the telemetry checks of a recorded run (default: the latest) without sending traffic |
| `report [RUN_ID]` | Prints a recorded run (default: the latest) |
| `reset [--force]` | Restores interrupted runs, removes all fault rules and closes all circuit breakers; `--force` also releases a lock held by another process |

Every run writes two files under `.runs/<run id>/`:

| File | Contents |
| --- | --- |
| `record.json` | Every request (status, duration, correlation id, Kong request id, trace id, order id), every order read back, the Kubernetes actions, the faults, the metric snapshot and every check result |
| `journal.jsonl` | Every change made to the platform, written before the change ([experiment-safety.md](experiment-safety.md)) |

The run id has the form `<UTC timestamp>-<scenario>-<random suffix>`, for example
`20260918T001359Z-payment-retry-cd2d`. The output format is explained in
[scenario-catalog.md](scenario-catalog.md#reading-a-runs-output).

## Behavior worth knowing

### Deployments are idempotent

Every step checks what already exists and changes only what differs. Running `deploy` twice in a row
changes nothing the second time. Two mechanisms make this true for the parts that usually are not:

- **Content-based image tags.** Each service image is tagged with the first 12 characters of a
  SHA-256 hash over its source files, the shared `ServiceDefaults` library and the build files. An
  unchanged service keeps its tag, so it is neither rebuilt nor restarted; any change produces a new
  tag and a rolling update. Line endings are normalized first, so a Windows and a Linux checkout of
  the same commit produce the same tag.
- **Generate-once secrets and certificates.** Generated secrets are created only if missing. The
  local CA (valid one year) and the server certificate (valid 90 days) are reused until fewer than
  14 days of validity remain; the server certificate is also reissued if the CA changed or a
  required host name is missing.

The `recovery` validation suite deploys every component again and verifies that not a single pod was
replaced or restarted.

### Interrupted deployments are recovered

If a `helm upgrade` is killed, Helm leaves its release in a *pending* state and refuses every later
change to it. Before each release, the automation checks for this:

| Release state | Action |
| --- | --- |
| Pending for less than 20 minutes | Stop with a message: another deployment may still be running |
| Pending for 20 minutes or more (longer than any Helm timeout the automation uses) | A pending first install is removed (persistent volumes stay); a pending upgrade or rollback is rolled back to the last working revision |

### The cluster is always the local one

Every call names the `kind-txplatform` context explicitly and passes a guard that requires a loopback
API server ([experiment-safety.md](experiment-safety.md#the-context-guard)). The current kubectl
context is never used.

### One operation at a time

`bootstrap`, `deploy`, `validate` and `scenarioctl run` all take the same cluster-wide lock, so a
deployment cannot restart pods in the middle of an experiment, and two experiments cannot spoil each
other's measurements. A refused command names the current holder.

### Child processes are cleaned up

`kubectl port-forward` processes are started for each access to a cluster-internal port and stopped
afterwards — including their whole process tree. This matters on Windows, where package managers
such as Chocolatey install `kubectl` as a small launcher that starts the real program as a child
process; stopping only the launcher would leave the port-forward running.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Something failed: a check, a deployment step, a refused lock, an invalid scenario |
| `130` | Interrupted with Ctrl+C (`scenarioctl` restores the platform first) |

`scenarioctl run` returns `1` if any check of any repetition failed, so both tools can be used in
scripts and pipelines.

## Related documents

- [Setup guide](setup-guide.md) — the first bootstrap, step by step
- [Operations](operations.md) — everyday tasks built from these commands
- [Validation suites](validation-suites.md) — what `validate` checks
- [Scenario catalog](scenario-catalog.md) — what `scenarioctl run` can run
