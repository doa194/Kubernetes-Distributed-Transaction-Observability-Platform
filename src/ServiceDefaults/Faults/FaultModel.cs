using System.Collections.Concurrent;
using System.Text.Json.Serialization;

namespace ServiceDefaults.Faults;

// Kinds of simulated faults a service can be told to produce.
public enum FaultMode
{
    [JsonStringEnumMemberName("latency")]
    Latency,
    [JsonStringEnumMemberName("http-error")]
    HttpError,
    [JsonStringEnumMemberName("timeout")]
    Timeout,
    [JsonStringEnumMemberName("intermittent")]
    Intermittent,
    [JsonStringEnumMemberName("business-rejection")]
    BusinessRejection,
    [JsonStringEnumMemberName("readiness-loss")]
    ReadinessLoss,
}

// Where in a handler the fault runs: before the business effect (nothing is recorded yet) or
// after it (the effect is recorded, only the response is delayed). "After" is how a lost
// response is simulated, e.g. a payment that was authorized but whose reply never arrived.
public enum FaultPhase
{
    [JsonStringEnumMemberName("before-effect")]
    BeforeEffect,
    [JsonStringEnumMemberName("after-effect")]
    AfterEffect,
}

// What the handler must do after the fault logic ran.
public enum FaultOutcome
{
    Continue,
    Fail,
    Reject,
}

public readonly record struct FaultDecision(FaultOutcome Outcome, int StatusCode = 0)
{
    public static readonly FaultDecision Continue = new(FaultOutcome.Continue);

    public static FaultDecision Fail(int statusCode) => new(FaultOutcome.Fail, statusCode);

    public static readonly FaultDecision Reject = new(FaultOutcome.Reject);
}

// A fault rule as sent by an operator (for example the scenario runner).
public sealed record FaultRuleRequest(
    string Operation,
    FaultMode Mode,
    int TtlSeconds,
    int? DelayMs = null,
    int? StatusCode = null,
    FaultPhase? Phase = null,
    int? FailAttempts = null,
    int? EveryNth = null,
    string? ScenarioRunId = null,
    int? MaxActivations = null);

// An active fault rule. Every rule expires, so a forgotten or interrupted experiment
// always heals itself even if nobody clears it.
public sealed class FaultRule
{
    private int activations;
    private int requestCount;

    public required string Id { get; init; }

    public required string Operation { get; init; }

    public required FaultMode Mode { get; init; }

    public int? DelayMs { get; init; }

    public int? StatusCode { get; init; }

    public FaultPhase Phase { get; init; } = FaultPhase.BeforeEffect;

    public int? FailAttempts { get; init; }

    public int? EveryNth { get; init; }

    public string? ScenarioRunId { get; init; }

    public int? MaxActivations { get; init; }

    public required DateTimeOffset CreatedAt { get; init; }

    public required DateTimeOffset ExpiresAt { get; init; }

    public int Activations => activations;

    // Attempts seen per key (for example per idempotency key), used by intermittent faults.
    internal ConcurrentDictionary<string, int> AttemptsPerKey { get; } = new(StringComparer.Ordinal);

    public bool IsExpired(DateTimeOffset now) => now >= ExpiresAt;

    public bool IsExhausted => MaxActivations is { } max && activations >= max;

    internal int RecordActivation() => Interlocked.Increment(ref activations);

    internal int NextRequestNumber() => Interlocked.Increment(ref requestCount);
}
