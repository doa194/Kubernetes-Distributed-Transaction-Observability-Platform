# Documentation

This folder describes the platform as it is built and verified: how it works, why it was designed
this way, how to operate it, and where its limits are. The documents are organised around the
questions people ask, not around technologies or folders.

## Where to start

Pick the path that matches your goal. Each path is a short reading order; everything else can be
looked up when needed.

| Your goal | Read, in this order |
| --- | --- |
| **Understand the project quickly** | [Walkthrough](walkthrough.md) → [Architecture](architecture.md) → [Scenario catalog](scenario-catalog.md) |
| **Install and run it** | [Setup guide](setup-guide.md) → [Automation CLI](automation-cli.md) → [Troubleshooting](troubleshooting.md) |
| **Learn how the observability works** | [Tracing](tracing.md) → [Telemetry pipeline](telemetry-pipeline.md) → [Sampling](sampling.md) → [Metrics and monitoring](metrics-and-monitoring.md) |
| **Review the engineering decisions** | [Design decisions](design-decisions.md) → [Known limitations](known-limitations.md) → [Production considerations](production-considerations.md) |
| **Run or write experiments** | [Scenario catalog](scenario-catalog.md) → [Fault injection](fault-injection.md) → [Writing scenarios](writing-scenarios.md) → [Telemetry contracts](telemetry-contracts.md) |
| **Change the code** | [Codebase guide](codebase-guide.md) → [Testing strategy](testing-strategy.md) → [Configuration reference](configuration-reference.md) |

Unfamiliar terms are explained in the [glossary](glossary.md).

## All documents

### Getting started

| Document | What it covers |
| --- | --- |
| [Walkthrough](walkthrough.md) | A guided first hour: send an order, read its trace, look at the metrics, run an experiment |
| [Setup guide](setup-guide.md) | Prerequisites, installation, verification and cleanup, step by step |
| [Glossary](glossary.md) | Plain-language explanations of every important term |

### The system

| Document | What it covers |
| --- | --- |
| [Architecture](architecture.md) | Components, the path of a request, the path of its telemetry, and the reasons behind the shape |
| [Services](services.md) | What each of the five services does, the state it keeps and the rules it applies |
| [Transaction flow](transaction-flow.md) | The order's states, how failures are classified and what is undone |
| [Resilience](resilience.md) | Timeouts, retries, circuit breakers, idempotent payments and graceful shutdown |
| [API reference](api-reference.md) | The public Order API, the internal service APIs and the management API |

### Platform and security

| Document | What it covers |
| --- | --- |
| [Kubernetes platform](kubernetes-platform.md) | The cluster, namespaces, workload settings, Helm charts and storage |
| [Gateway](gateway.md) | Kong, the Gateway API resources, edge policies and the trace root |
| [Identity and tokens](identity-and-tokens.md) | The Keycloak realm, clients, permissions and token validation |
| [Security](security.md) | Trust boundaries, pod security, secrets and data that must never be stored |
| [Network policies](network-policies.md) | Which workload may talk to which, and how that is proven |

### Observability

| Document | What it covers |
| --- | --- |
| [Tracing](tracing.md) | How a request becomes a trace, and the conventions on every span |
| [Telemetry pipeline](telemetry-pipeline.md) | Everything the Collector does to a span, step by step |
| [Sampling](sampling.md) | Which traces are stored, why, and how the rate is verified |
| [Metrics and monitoring](metrics-and-monitoring.md) | Metrics derived from spans, Prometheus, and Jaeger's Monitor tab |
| [Trace storage](trace-storage.md) | OpenSearch indices, retention and the query API |

### Experiments and verification

| Document | What it covers |
| --- | --- |
| [Scenario catalog](scenario-catalog.md) | The 18 experiments, what each proves and what was measured |
| [Fault injection](fault-injection.md) | How failures are produced precisely and safely |
| [Writing scenarios](writing-scenarios.md) | The scenario file format and how to add a telemetry contract |
| [Telemetry contracts](telemetry-contracts.md) | How the platform checks that its telemetry tells the truth |
| [Experiment safety](experiment-safety.md) | The experiment lock, the journal, automatic restore and fault expiry |
| [Validation suites](validation-suites.md) | The 42 default and 5 disruptive operational checks, and what each one proves |
| [Testing strategy](testing-strategy.md) | Every test layer, what it protects and how to run it |

### Operating the platform

| Document | What it covers |
| --- | --- |
| [Automation CLI](automation-cli.md) | Reference for `platformctl` and `scenarioctl` |
| [Operations](operations.md) | Day-to-day tasks: find a request, redeploy a part, change a setting |
| [Configuration reference](configuration-reference.md) | Every setting that matters and the file it lives in |
| [Versions and upgrades](versions-and-upgrades.md) | Pinned versions, compatibility constraints and how to upgrade |
| [Troubleshooting](troubleshooting.md) | Symptoms, causes and fixes |

### Judgement

| Document | What it covers |
| --- | --- |
| [Design decisions](design-decisions.md) | Each major choice with its alternatives, benefits and costs |
| [Known limitations](known-limitations.md) | Behaviors and constraints to know before trusting a trace or reusing a setting |
| [Production considerations](production-considerations.md) | What is simplified locally and what production would need |
| [Codebase guide](codebase-guide.md) | Where every part of the code lives and which file answers which question |

## Conventions used in these documents

- **Commands are run from the repository root.** The automation is started with `uv run`, which
  uses the project's locked Python environment; nothing needs to be activated first.
- **Shell examples use bash.** On Windows they work unchanged in Git Bash, with one exception noted
  where it matters: curl on Windows — Git Bash's included — needs `--ssl-revoke-best-effort` to
  trust the local certificate authority.
- **Numbers are measured, not estimated.** Where a document quotes a result ("240 of 240 orders"),
  it comes from a real run on the local cluster. Where results vary between runs, the variation is
  stated.
- **Names in `code font`** are exact identifiers: file paths, span names, attribute keys, command
  flags, Kubernetes resource names.
- **"Today" versus "production".** Anything described as present is implemented and verified.
  Recommendations for a production deployment appear only in clearly marked sections and in
  [production-considerations.md](production-considerations.md).
