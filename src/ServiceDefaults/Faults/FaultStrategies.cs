namespace ServiceDefaults.Faults;

// Strategy pattern: one small class per fault mode, so fault behavior never leaks into handlers.
// Handlers only ask the injector for a decision at the points where faults may apply.
public interface IFaultStrategy
{
    FaultMode Mode { get; }

    ValueTask<FaultDecision> ExecuteAsync(FaultRule rule, string? key, CancellationToken cancellationToken);
}

internal sealed class LatencyStrategy(TimeProvider timeProvider) : IFaultStrategy
{
    public FaultMode Mode => FaultMode.Latency;

    public async ValueTask<FaultDecision> ExecuteAsync(FaultRule rule, string? key, CancellationToken cancellationToken)
    {
        await Task.Delay(TimeSpan.FromMilliseconds(rule.DelayMs!.Value), timeProvider, cancellationToken);
        return FaultDecision.Continue;
    }
}

internal sealed class HttpErrorStrategy : IFaultStrategy
{
    public FaultMode Mode => FaultMode.HttpError;

    public ValueTask<FaultDecision> ExecuteAsync(FaultRule rule, string? key, CancellationToken cancellationToken) =>
        ValueTask.FromResult(FaultDecision.Fail(rule.StatusCode!.Value));
}

// Holds the request until the caller gives up (its timeout cancels the request), so the caller
// experiences a real timeout. If nobody cancels within the hold time, it answers 504.
internal sealed class TimeoutStrategy(TimeProvider timeProvider) : IFaultStrategy
{
    private const int DefaultHoldMs = 60_000;

    public FaultMode Mode => FaultMode.Timeout;

    public async ValueTask<FaultDecision> ExecuteAsync(FaultRule rule, string? key, CancellationToken cancellationToken)
    {
        await Task.Delay(TimeSpan.FromMilliseconds(rule.DelayMs ?? DefaultHoldMs), timeProvider, cancellationToken);
        return FaultDecision.Fail(504);
    }
}

// Deterministic, not random: either the first N attempts per key fail (so a retry succeeds
// predictably), or every Nth request fails. Repeated scenario runs therefore behave the same.
internal sealed class IntermittentStrategy : IFaultStrategy
{
    public FaultMode Mode => FaultMode.Intermittent;

    public ValueTask<FaultDecision> ExecuteAsync(FaultRule rule, string? key, CancellationToken cancellationToken)
    {
        var shouldFail = rule.FailAttempts is { } failAttempts
            ? rule.AttemptsPerKey.AddOrUpdate(key ?? string.Empty, 1, (_, attempts) => attempts + 1) <= failAttempts
            : rule.NextRequestNumber() % rule.EveryNth!.Value == 0;

        return ValueTask.FromResult(shouldFail ? FaultDecision.Fail(rule.StatusCode!.Value) : FaultDecision.Continue);
    }
}

internal sealed class BusinessRejectionStrategy : IFaultStrategy
{
    public FaultMode Mode => FaultMode.BusinessRejection;

    public ValueTask<FaultDecision> ExecuteAsync(FaultRule rule, string? key, CancellationToken cancellationToken) =>
        ValueTask.FromResult(FaultDecision.Reject);
}
