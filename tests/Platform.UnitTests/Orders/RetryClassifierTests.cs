using OrderService.Infrastructure.Dependencies;

namespace Platform.UnitTests.Orders;

// Retry rules differ per dependency. Getting them wrong either hides failures (too many retries)
// or causes unsafe duplicates (retrying something that is not idempotent).
public sealed class RetryClassifierTests
{
    private static CallOutcome Status(int code) => new(OutcomeCategory.HttpStatus, code);

    [Theory]
    [InlineData(502)]
    [InlineData(503)]
    [InlineData(504)]
    public void Gateway_style_errors_are_retried(int status)
    {
        Assert.True(RetryClassifier.ShouldRetry(DependencyProfiles.Inventory, Status(status)));
    }

    [Theory]
    [InlineData(400)]
    [InlineData(401)]
    [InlineData(403)]
    [InlineData(422)]
    [InlineData(500)]
    public void Client_errors_business_rejections_and_generic_server_errors_are_not_retried(int status)
    {
        Assert.False(RetryClassifier.ShouldRetry(DependencyProfiles.Payment, Status(status)));
    }

    [Fact]
    public void Only_payment_waits_for_an_in_progress_request()
    {
        Assert.True(RetryClassifier.ShouldRetry(DependencyProfiles.Payment, Status(409)));
        Assert.False(RetryClassifier.ShouldRetry(DependencyProfiles.Shipping, Status(409)));
    }

    [Fact]
    public void Timeouts_and_connection_failures_are_retried_but_open_circuits_are_not()
    {
        Assert.True(RetryClassifier.ShouldRetry(DependencyProfiles.Fraud, new CallOutcome(OutcomeCategory.AttemptTimeout)));
        Assert.True(RetryClassifier.ShouldRetry(DependencyProfiles.Fraud, new CallOutcome(OutcomeCategory.ConnectionFailure)));
        Assert.False(RetryClassifier.ShouldRetry(DependencyProfiles.Fraud, new CallOutcome(OutcomeCategory.CircuitOpen)));
    }

    [Fact]
    public void Only_technical_failures_count_against_the_circuit_breaker()
    {
        Assert.True(RetryClassifier.CountsAsBreakerFailure(Status(500)));
        Assert.True(RetryClassifier.CountsAsBreakerFailure(new CallOutcome(OutcomeCategory.AttemptTimeout)));
        Assert.False(RetryClassifier.CountsAsBreakerFailure(Status(422)));
    }

    [Fact]
    public void Every_stage_budget_leaves_room_for_all_attempts()
    {
        // If a budget were shorter than its attempts, the last retries would silently never happen.
        foreach (var profile in DependencyProfiles.All)
        {
            var backoff = TimeSpan.FromMilliseconds(200 * (Math.Pow(2, profile.MaxAttempts - 1) - 1));
            var worstCase = profile.AttemptTimeout * profile.MaxAttempts + backoff;
            Assert.True(worstCase <= profile.StageBudget, $"{profile.Name}: {worstCase} does not fit {profile.StageBudget}");
        }
    }
}
