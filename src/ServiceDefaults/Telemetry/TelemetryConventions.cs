namespace ServiceDefaults.Telemetry;

// Attribute, event and baggage names shared by all services.
// Keeping them in one place stops services from drifting apart, which would break trace
// searches and the automated trace contracts that look for these exact names.
public static class TelemetryConventions
{
    // Business identifiers. They appear on spans only, never as metric dimensions.
    public const string OrderId = "order.id";
    public const string OrderState = "order.state";
    public const string ScenarioId = "scenario.id";
    public const string ScenarioRunId = "scenario.run_id";
    public const string CorrelationId = "correlation.id";
    public const string KongRequestId = "kong.request.id";

    // Retry and payment attempts.
    public const string RetryCount = "retry.count";
    public const string RetryAttempt = "retry.attempt";
    public const string PaymentAttempt = "payment.attempt";
    public const string PaymentIdempotentReplay = "payment.idempotent_replay";

    // Failures and simulated faults.
    public const string FailureStage = "failure.stage";
    public const string FailureKind = "failure.kind";
    public const string FailureReason = "failure.reason";
    public const string FailureInjected = "failure.injected";
    public const string SimulationMode = "simulation.mode";
    public const string FaultRuleId = "fault.rule.id";

    // Marks a trace that must be kept by tail sampling.
    public const string SamplingDebug = "sampling.debug";

    // Span events.
    public const string EventStateChanged = "order.state_changed";
    public const string EventRetry = "retry";
    public const string EventCircuitStateChanged = "circuit.state_changed";
    public const string EventFaultApplied = "fault.applied";
    public const string EventCompensationCompleted = "compensation.completed";
    public const string EventCompensationFailed = "compensation.failed";

    // Only these baggage keys cross service boundaries and become span attributes.
    public static readonly IReadOnlySet<string> AllowedBaggageKeys = new HashSet<string>(StringComparer.Ordinal)
    {
        ScenarioId,
        ScenarioRunId,
        CorrelationId,
    };
}
