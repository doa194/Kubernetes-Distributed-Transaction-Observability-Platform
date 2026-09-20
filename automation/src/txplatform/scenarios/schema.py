"""Validated structure of a scenario file (scenarios/*.yaml).

Scenarios describe intent - which faults to inject, which traffic to send, what must happen - and
the runner turns that into actions. Strict validation (unknown fields rejected, required expiry,
consistent parameters) stops a typo from silently producing a different experiment.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DOWNSTREAM_SERVICES = ("inventory-service", "fraud-service", "payment-service", "shipping-service")
# Workloads a scenario may disrupt: name -> (namespace, kind). Keycloak and Kong are left out on
# purpose: without them no request could be authenticated or routed at all.
WORKLOADS: dict[str, tuple[str, str]] = {
    **{name: ("transaction-platform", "Deployment") for name in ("order-service", *DOWNSTREAM_SERVICES)},
    "jaeger-collector": ("observability", "Deployment"),
    "opensearch-traces": ("observability", "StatefulSet"),
}
FAULT_OPERATIONS = {
    "inventory-service": {"inventory.reserve", "inventory.release", "readiness"},
    "fraud-service": {"fraud.evaluate", "readiness"},
    "payment-service": {"payment.authorize", "payment.void", "readiness"},
    "shipping-service": {"shipping.create", "readiness"},
}
MAX_FAULT_TTL_SECONDS = 1800


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Category(StrEnum):
    APPLICATION_FAULT = "application-fault"
    KUBERNETES_FAULT = "kubernetes-fault"
    EDGE = "edge"
    SECURITY = "security"
    WORKLOAD = "workload"


class FaultSpec(_Strict):
    service: Literal["inventory-service", "fraud-service", "payment-service", "shipping-service"]
    operation: str
    mode: Literal["latency", "http-error", "timeout", "intermittent", "business-rejection", "readiness-loss"]
    delay_ms: int | None = Field(default=None, alias="delayMs", ge=1, le=120_000)
    status_code: int | None = Field(default=None, alias="statusCode", ge=400, le=599)
    phase: Literal["before-effect", "after-effect"] | None = None
    fail_attempts: int | None = Field(default=None, alias="failAttempts", ge=1)
    every_nth: int | None = Field(default=None, alias="everyNth", ge=2)
    max_activations: int | None = Field(default=None, alias="maxActivations", ge=1)
    # Every fault expires even if cleanup never runs.
    ttl_seconds: int = Field(alias="ttlSeconds", ge=10, le=MAX_FAULT_TTL_SECONDS)
    # "all" affects every replica; "one" affects a single replica (replica-specific degradation).
    replicas: Literal["all", "one"] = "all"
    # Scoped faults only affect requests of this scenario run; readiness faults cannot be scoped.
    scoped: bool = True

    @model_validator(mode="after")
    def check_parameters(self) -> FaultSpec:
        if self.operation not in FAULT_OPERATIONS[self.service]:
            raise ValueError(f"{self.service} has no operation '{self.operation}'")
        if (self.mode == "readiness-loss") != (self.operation == "readiness"):
            raise ValueError("readiness-loss faults must use the 'readiness' operation, and only they may")
        if self.mode == "readiness-loss" and self.scoped:
            raise ValueError("readiness-loss faults affect a whole pod and must set scoped: false")
        if self.mode == "latency" and self.delay_ms is None:
            raise ValueError("latency faults need delayMs")
        if self.mode in {"http-error", "intermittent"} and self.status_code is None:
            raise ValueError(f"{self.mode} faults need statusCode")
        if self.mode == "intermittent" and (self.fail_attempts is None) == (self.every_nth is None):
            raise ValueError("intermittent faults need exactly one of failAttempts or everyNth")
        if self.phase == "after-effect" and self.mode != "latency":
            raise ValueError("only latency faults can run after the business effect")
        return self

    def to_rule(self, scenario_run_id: str) -> dict:
        rule = {
            "operation": self.operation, "mode": self.mode, "ttlSeconds": self.ttl_seconds,
            "delayMs": self.delay_ms, "statusCode": self.status_code, "phase": self.phase,
            "failAttempts": self.fail_attempts, "everyNth": self.every_nth, "maxActivations": self.max_activations,
            "scenarioRunId": scenario_run_id if self.scoped else None,
        }
        return {key: value for key, value in rule.items() if value is not None}


class KubernetesAction(_Strict):
    action: Literal["delete-pod", "rollout-restart", "scale"]
    workload: str
    # Seconds after the workload started.
    at_seconds: float = Field(default=0, alias="atSeconds", ge=0, le=600)
    replicas: int | None = Field(default=None, ge=0, le=5)
    # For scale: restore the original replica count this many seconds after scaling (cleanup always restores).
    restore_after_seconds: float | None = Field(default=None, alias="restoreAfterSeconds", ge=1, le=600)

    @field_validator("workload")
    @classmethod
    def known_workload(cls, value: str) -> str:
        if value not in WORKLOADS:
            raise ValueError(f"workload must be one of {sorted(WORKLOADS)}")
        return value

    @model_validator(mode="after")
    def check_replicas(self) -> KubernetesAction:
        if (self.action == "scale") != (self.replicas is not None):
            raise ValueError("replicas is required for scale and only allowed for scale")
        if self.restore_after_seconds is not None and self.action != "scale":
            raise ValueError("restoreAfterSeconds only applies to scale")
        return self


class Profile(StrEnum):
    SINGLE = "single"
    CONCURRENT = "concurrent"
    BURST = "burst"
    STEADY = "steady"
    SPIKE = "spike"


OrderTemplate = Literal["normal", "high-risk", "declined", "restricted-zone", "out-of-stock", "oversized", "malformed"]
Identity = Literal["scenario-runner", "order-reader", "none", "tampered"]


class WorkloadStep(_Strict):
    profile: Profile
    requests: int = Field(default=1, ge=1, le=5000)
    concurrency: int = Field(default=1, ge=1, le=200)
    rate_per_second: float | None = Field(default=None, alias="ratePerSecond", gt=0, le=200)
    duration_seconds: float | None = Field(default=None, alias="durationSeconds", gt=0, le=600)
    spike_rate_per_second: float | None = Field(default=None, alias="spikeRatePerSecond", gt=0, le=200)
    spike_seconds: float | None = Field(default=None, alias="spikeSeconds", gt=0, le=120)
    order: OrderTemplate = "normal"
    identity: Identity = "scenario-runner"
    debug: bool = False
    # Wait this long before the step starts, e.g. until a Kubernetes action has taken effect.
    delay_seconds: float = Field(default=0, alias="delaySeconds", ge=0, le=300)
    label: str = ""

    @model_validator(mode="after")
    def check_profile(self) -> WorkloadStep:
        needs_rate = self.profile in {Profile.STEADY, Profile.SPIKE}
        if needs_rate and (self.rate_per_second is None or self.duration_seconds is None):
            raise ValueError(f"{self.profile} needs ratePerSecond and durationSeconds")
        if self.profile == Profile.SPIKE and (self.spike_rate_per_second is None or self.spike_seconds is None):
            raise ValueError("spike needs spikeRatePerSecond and spikeSeconds")
        if self.profile == Profile.SPIKE and self.spike_seconds >= self.duration_seconds:  # type: ignore[operator]
            raise ValueError("spikeSeconds must be shorter than durationSeconds")
        if self.debug and self.identity != "scenario-runner":
            raise ValueError("only the scenario-runner identity holds the traces.debug permission")
        return self


class CountRule(_Strict):
    exactly: int | None = Field(default=None, ge=0)
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    all: bool = False

    @model_validator(mode="after")
    def one_kind(self) -> CountRule:
        if self.all and any(v is not None for v in (self.exactly, self.min, self.max)):
            raise ValueError("'all' cannot be combined with exactly/min/max")
        if not self.all and all(v is None for v in (self.exactly, self.min, self.max)):
            raise ValueError("a count rule needs exactly, min, max or all")
        return self


class ApplicationExpectations(_Strict):
    # HTTP status -> how many responses must have it. Statuses not listed must not occur.
    statuses: dict[int, CountRule]
    # Final order state -> how many created orders must end in it (read back with GET /orders/{id}).
    states: dict[str, CountRule] = Field(default_factory=dict)
    # Compensation steps every failed order must have completed, e.g. [VoidPayment, ReleaseInventory].
    compensation: list[str] | None = None
    # Exact number of payment authorizations recorded per created order.
    authorizations_per_order: int | None = Field(default=None, alias="authorizationsPerOrder", ge=0, le=5)


class TelemetryExpectations(_Strict):
    contract: str
    parameters: dict[str, float | int | str | bool | list[str]] = Field(default_factory=dict)


class Expectations(_Strict):
    application: ApplicationExpectations
    telemetry: TelemetryExpectations | None = None


class Scenario(_Strict):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")]
    title: str
    description: str
    category: Category
    faults: list[FaultSpec] = Field(default_factory=list)
    kubernetes: list[KubernetesAction] = Field(default_factory=list)
    workload: list[WorkloadStep] = Field(min_length=1)
    expect: Expectations
