using Polly;
using Polly.CircuitBreaker;
using Polly.Timeout;
using ServiceDefaults.Telemetry;

namespace OrderService.Infrastructure.Dependencies;

// Resilience settings of one dependency. The values differ on purpose: each dependency has a
// different cost of retrying (reads are cheap, payments move money) and a different latency.
public sealed record DependencyProfile(
    string Name,
    TimeSpan AttemptTimeout,
    int MaxAttempts,
    TimeSpan StageBudget,
    bool UseCircuitBreaker,
    bool RetryOnConflictInProgress);

public static class DependencyProfiles
{
    public static readonly DependencyProfile Inventory = new("inventory", TimeSpan.FromSeconds(1), 2, TimeSpan.FromSeconds(2.5), true, false);
    public static readonly DependencyProfile Fraud = new("fraud", TimeSpan.FromMilliseconds(700), 2, TimeSpan.FromSeconds(1.8), true, false);

    // Payment retries are only safe because every attempt carries the same Idempotency-Key.
    // 409 means the first attempt is still being processed, which is worth waiting for.
    public static readonly DependencyProfile Payment = new("payment", TimeSpan.FromSeconds(1.5), 3, TimeSpan.FromSeconds(5.5), true, true);
    public static readonly DependencyProfile Shipping = new("shipping", TimeSpan.FromSeconds(1), 3, TimeSpan.FromSeconds(4), true, false);

    // Undo calls are short and idempotent; they run after a failure and must not stretch the response time.
    public static readonly DependencyProfile InventoryCompensation = new("inventory-compensation", TimeSpan.FromMilliseconds(500), 2, TimeSpan.FromSeconds(1.2), false, false);
    public static readonly DependencyProfile PaymentCompensation = new("payment-compensation", TimeSpan.FromMilliseconds(500), 2, TimeSpan.FromSeconds(1.2), false, false);

    public static readonly DependencyProfile[] All = [Inventory, Fraud, Payment, Shipping, InventoryCompensation, PaymentCompensation];
}

public enum OutcomeCategory
{
    Success,
    HttpStatus,
    AttemptTimeout,
    ConnectionFailure,
    CircuitOpen,
    Cancelled,
    Other,
}

public readonly record struct CallOutcome(OutcomeCategory Category, int StatusCode = 0)
{
    public string Describe() => Category == OutcomeCategory.HttpStatus ? $"http_{StatusCode}" : Category.ToString().ToLowerInvariant();

    public static CallOutcome From(Outcome<HttpResponseMessage> outcome) => outcome.Exception switch
    {
        null when outcome.Result is { IsSuccessStatusCode: true } => new(OutcomeCategory.Success),
        null when outcome.Result is not null => new(OutcomeCategory.HttpStatus, (int)outcome.Result.StatusCode),
        TimeoutRejectedException => new(OutcomeCategory.AttemptTimeout),
        HttpRequestException => new(OutcomeCategory.ConnectionFailure),
        BrokenCircuitException => new(OutcomeCategory.CircuitOpen),
        OperationCanceledException => new(OutcomeCategory.Cancelled),
        _ => new(OutcomeCategory.Other),
    };
}

// Pure decisions about which outcomes are worth retrying and which count against the circuit breaker.
public static class RetryClassifier
{
    public static bool ShouldRetry(DependencyProfile profile, CallOutcome outcome) => outcome.Category switch
    {
        OutcomeCategory.AttemptTimeout or OutcomeCategory.ConnectionFailure => true,
        OutcomeCategory.HttpStatus => outcome.StatusCode is 502 or 503 or 504
            || (outcome.StatusCode == 409 && profile.RetryOnConflictInProgress),
        // Business rejections (4xx), open circuits and cancellations never improve by retrying.
        _ => false,
    };

    public static bool CountsAsBreakerFailure(CallOutcome outcome) => outcome.Category switch
    {
        OutcomeCategory.AttemptTimeout or OutcomeCategory.ConnectionFailure => true,
        OutcomeCategory.HttpStatus => outcome.StatusCode >= 500,
        _ => false,
    };
}

// Adds the attempt number to each outgoing request. It sits inside the retry strategy, so it
// runs once per attempt, and downstream services can record which attempt they received.
public sealed class AttemptNumberHandler : DelegatingHandler
{
    public const string HeaderName = "X-Attempt";

    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var attempt = request.Options.TryGetValue(HttpAttempt.OptionsKey, out var previous) ? previous + 1 : 1;
        request.Options.Set(HttpAttempt.OptionsKey, attempt);
        request.Headers.Remove(HeaderName);
        request.Headers.TryAddWithoutValidation(HeaderName, attempt.ToString(System.Globalization.CultureInfo.InvariantCulture));
        return base.SendAsync(request, cancellationToken);
    }
}

// Manual controls let an operator close every circuit breaker, so one scenario's outage cannot
// leak into the next scenario through a breaker that is still open.
public sealed class CircuitBreakerControls
{
    private readonly Dictionary<string, CircuitBreakerManualControl> controls =
        DependencyProfiles.All.Where(p => p.UseCircuitBreaker).ToDictionary(p => p.Name, _ => new CircuitBreakerManualControl());

    public CircuitBreakerManualControl For(string dependency) => controls[dependency];

    public async Task<int> CloseAllAsync(CancellationToken cancellationToken)
    {
        foreach (var control in controls.Values)
        {
            await control.CloseAsync(cancellationToken);
        }

        return controls.Count;
    }
}
