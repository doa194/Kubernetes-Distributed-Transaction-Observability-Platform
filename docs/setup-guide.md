# Setup guide

This guide takes a workstation from nothing to a running, verified platform, and back to nothing.
Every step is a command you can copy; there is nothing to configure by hand.

**What you will have at the end:** a three-node Kubernetes cluster on your machine running Kong,
Keycloak, five .NET services, Jaeger, OpenSearch and Prometheus — all verified by 32 automated smoke
checks — plus the tools to run experiments against it.

---

## 1. Requirements

### Hardware

The platform runs thirteen pods on three cluster nodes — among them OpenSearch and Keycloak, the
two largest — so it needs a reasonably sized Docker installation. These limits are checked
automatically in step 3.

| Resource | Minimum | Recommended | Why |
| --- | --- | --- | --- |
| CPUs available to Docker | 4 | 6 | Bootstrap builds five images and starts every component |
| Memory available to Docker | 8 GB | 10 GB | OpenSearch alone reserves 1.5 GB |
| Free disk space | – | 30 GB | Container images, two persistent volumes, build caches |

On Docker Desktop, these values are set under *Settings → Resources*.

### Software

| Tool | Version | Used for |
| --- | --- | --- |
| [Docker](https://docs.docker.com/get-docker/) | any current release, running | Runs the cluster nodes and builds images |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | v0.33.x | Creates the local Kubernetes cluster |
| [Helm](https://helm.sh/docs/intro/install/) | v4 | Installs the charts |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | 1.35–1.37 | Talks to the cluster; also used for port-forwards |
| [.NET SDK](https://dotnet.microsoft.com/download) | 10.x | Builds the services and runs their tests |
| [Python](https://www.python.org/downloads/) | 3.12 or newer | Runs the automation |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | any recent version | Installs the automation's locked dependencies |

The automation looks for kind, Helm, kubectl and Docker on your `PATH` first and then in the project
folder `.tools/bin` (ignored by git), so kind and Helm can simply be placed there instead of being
installed system-wide. `uv` is started by you rather than by the automation, so it must be on your
`PATH`. If you keep it in `.tools/bin` too, add that folder to the `PATH` of your shell after
changing into the repository folder (step 2):

```bash
export PATH="$PWD/.tools/bin:$PATH"        # Git Bash, Linux, macOS
```

```powershell
$env:PATH = "$PWD\.tools\bin;$env:PATH"    # PowerShell
```

### Network

Two ports must be free on `127.0.0.1`:

| Port | Used by |
| --- | --- |
| 8443 | The Order API through Kong (HTTPS) |
| 9443 | Keycloak (HTTPS), for tokens |

Both are bound to loopback only; nothing is exposed to your network.

### Operating system notes

- **Windows with Docker Desktop** is the environment the platform is verified on. Run the bash
  examples in Git Bash (the `uv run` commands also work in PowerShell). curl on Windows — Git
  Bash's included — checks certificate revocation, which a local certificate authority cannot
  answer: add `--ssl-revoke-best-effort` to `curl` calls, as the examples in this documentation do.
- **Linux and macOS** are supported by every tool involved (Docker, kind, Helm, .NET, Python), but
  the platform has not been verified there. The `--ssl-revoke-best-effort` flag is not needed.

No environment variables need to be set. Everything the components need is generated or taken from
files in `deploy/` ([configuration-reference.md](configuration-reference.md)).

## 2. Get the code

```bash
git clone <repository-url> txplatform
cd txplatform
```

All following commands run from this folder. `uv run` creates the Python environment from
`uv.lock` on first use, so there is nothing to install or activate for the automation itself.

## 3. Check the workstation

```bash
uv run platformctl preflight
```

A healthy result looks like this:

```text
[ OK ] docker: server 29.8.0
[ OK ] docker-cpus: 12 CPUs allocated
[ OK ] docker-memory: 13.6 GiB allocated
[ OK ] kind: v0.33.0
[ OK ] helm: v4.3.0+gbec5b06
[ OK ] kubectl: client 1.36
[ OK ] dotnet-sdk: 10.0.401
[ OK ] python: 3.14
[ OK ] disk: 46 GiB free
[ OK ] port-8443: free
[ OK ] port-9443: free
```

| Result | Meaning | What to do |
| --- | --- | --- |
| `[ OK ]` | The requirement is met | Nothing |
| `[WARN]` | Below the recommendation, or a different tool version than the tested one | The platform will probably work; fix it if you see problems |
| `[FAIL]` | A hard requirement is missing | Fix it before bootstrapping |

## 4. Build the platform

```bash
uv run platformctl bootstrap
```

Bootstrap performs these steps in order. Each one is idempotent: running bootstrap again only
changes what actually differs.

| Step | What happens |
| --- | --- |
| Pre-flight | The checks from step 3 run again; any failure stops bootstrap |
| Cluster | A kind cluster named `txplatform` is created (one control-plane node, two workers) |
| `foundation` | Three namespaces with the strictest Pod Security level and deny-all network policies |
| `certificates` | A local certificate authority and a server certificate for `localhost` are generated into `.local/pki` |
| `crds` | The Gateway API and Kong resource definitions are installed |
| `gateway` | The Gateway, its HTTPS listener, the global tracing plugin and Kong's network policies |
| `kong` | Kong Gateway and the Kong Ingress Controller |
| `identity` | Keycloak with the `transaction-platform` realm; client secrets are generated |
| `observability` | OpenSearch, the Jaeger Collector and Query, Prometheus, and the retention job |
| `services` | The five service images are built, loaded into the cluster and deployed |
| Smoke validation | Five validation suites (32 checks) confirm the result |

Expected duration: **15–25 minutes** on the first run, most of it downloading images and compiling
the services, and **about 14 minutes** on a machine that already has the images. The last lines of
a successful run:

```text
==> Suite 'observability': OpenSearch storage, split Jaeger roles, Prometheus self-monitoring, retention, policies
[ OK ] retention job deletes trace indices of its own prefix only (8.4s): deleted retention-check-jaeger-span-…, kept txplatform-jaeger-span-…
[ OK ] only Collector, Query and the cleaner reach OpenSearch; Query and Prometheus stay private (38.4s): 7 connections behave as declared
==> 32 of 32 checks passed
```

To skip the smoke checks (not recommended), add `--skip-validation`.

## 5. Verify the platform

Run the full set of non-disruptive checks — 42 in total, including the telemetry pipeline — and look
at the overview:

```bash
uv run platformctl validate
uv run platformctl status
```

`validate` ends with `==> 42 of 42 checks passed`. What each check proves is described in
[validation-suites.md](validation-suites.md).

## 6. Use it

The [walkthrough](walkthrough.md) shows the first hour step by step. In short:

```bash
uv run platformctl ui jaeger             # traces:  http://127.0.0.1:16686
uv run platformctl ui prometheus         # metrics: http://127.0.0.1:9090
uv run scenarioctl list                  # the 18 experiments
uv run scenarioctl run normal-order      # run one and verify it
```

Each `ui` command holds a port-forward open until you press Ctrl+C.

## 7. Run the tests

```bash
dotnet test               # 151 .NET tests (unit, component, integration)
uv run pytest             # 121 Python unit tests
uv run pytest -m e2e      # 5 end-to-end tests; needs the running platform
```

The .NET integration tests start Keycloak in a container, so Docker must be running. Run
`dotnet test` without extra flags such as `--nologo`: the .NET SDK passes unknown flags on to the test
application, which then reports "Zero tests ran".

## 8. After changing code or configuration

Redeploy only what changed:

```bash
uv run platformctl deploy --component services        # after changing .NET code
uv run platformctl deploy --component observability   # after changing the Collector or storage
uv run platformctl deploy                             # everything (unchanged parts are left alone)
```

Service images are tagged with a hash of their source code, so unchanged services are neither
rebuilt nor restarted.

## 9. Clean up

```bash
uv run platformctl destroy           # delete the cluster; keep the local certificate authority
uv run platformctl destroy --purge   # also delete .local (certificates and generated state)
docker image rm $(docker image ls 'txplatform/*' -q)   # optional: remove the built service images
```

## What the platform leaves on your machine

| Location | Contents | Removed by |
| --- | --- | --- |
| kind cluster `txplatform` (Docker containers) | The whole platform | `platformctl destroy` |
| Your kubeconfig | The context `kind-txplatform` | `platformctl destroy` (kind removes it with the cluster) |
| `.local/pki/` | Local certificate authority and server certificate | `platformctl destroy --purge` |
| `.runs/` | One folder per experiment run (record and journal) | Delete the folder when no longer needed |
| `.venv/` | The automation's Python environment | Delete the folder |
| Helm's repository list | The entries `kong`, `opensearch` and `prometheus-community` | `helm repo remove kong opensearch prometheus-community` |
| Docker's image cache | The `txplatform/*` service images, the kind node image, and the images used by builds and tests | `docker image rm $(docker image ls 'txplatform/*' -q)` for the service images; `docker image rm <image>` for others |

The folders are ignored by git.

## If something goes wrong

| Symptom | First step |
| --- | --- |
| A pre-flight check fails | Follow its message; resources are set in Docker's settings |
| Bootstrap stops at a deployment | Run the same command again — steps are idempotent. See [troubleshooting.md](troubleshooting.md) |
| A validation check fails | The check's message names the cause; see [validation-suites.md](validation-suites.md) |
| `curl` refuses the certificate on Windows | Add `--ssl-revoke-best-effort` |

## Related documents

- [Walkthrough](walkthrough.md) — the first hour with the running platform
- [Automation CLI](automation-cli.md) — every command and option
- [Troubleshooting](troubleshooting.md) — symptoms and fixes
- [Versions and upgrades](versions-and-upgrades.md) — the pinned versions and why
