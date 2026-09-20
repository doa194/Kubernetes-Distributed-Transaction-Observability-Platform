using PaymentService.Domain;
using PaymentService.Infrastructure;

namespace Platform.UnitTests.Payments;

// Idempotency is what makes payment retries safe; these rules must never allow a second charge.
public sealed class IdempotencyRulesTests
{
    private const string Fingerprint = "ABC";

    [Fact]
    public void Unknown_key_starts_a_new_authorization()
    {
        Assert.Equal(IdempotencyDecision.StartNew, IdempotencyRules.Decide(null, Fingerprint));
    }

    [Fact]
    public void Finished_request_with_same_payload_is_replayed()
    {
        var record = new IdempotencyRecord("key", Fingerprint, IdempotencyStatus.Completed, new StoredResponse(201, "{}"));

        Assert.Equal(IdempotencyDecision.ReplayStored, IdempotencyRules.Decide(record, Fingerprint));
    }

    [Fact]
    public void Request_still_running_is_rejected_as_in_progress()
    {
        var record = new IdempotencyRecord("key", Fingerprint, IdempotencyStatus.InProgress, null);

        Assert.Equal(IdempotencyDecision.RejectInProgress, IdempotencyRules.Decide(record, Fingerprint));
    }

    [Fact]
    public void Same_key_with_different_payload_is_refused()
    {
        var record = new IdempotencyRecord("key", Fingerprint, IdempotencyStatus.Completed, new StoredResponse(201, "{}"));

        Assert.Equal(IdempotencyDecision.RejectFingerprintMismatch, IdempotencyRules.Decide(record, "OTHER"));
    }

    [Fact]
    public void Fingerprint_changes_when_amount_changes()
    {
        var orderId = Guid.NewGuid();

        Assert.Equal(
            IdempotencyRules.Fingerprint(orderId, 10m, "EUR", "tok_test_approved"),
            IdempotencyRules.Fingerprint(orderId, 10.00m, "EUR", "tok_test_approved"));
        Assert.NotEqual(
            IdempotencyRules.Fingerprint(orderId, 10m, "EUR", "tok_test_approved"),
            IdempotencyRules.Fingerprint(orderId, 11m, "EUR", "tok_test_approved"));
    }

    [Theory]
    [InlineData("tok_test_approved", true)]
    [InlineData("tok_test_declined", false)]
    [InlineData("tok_test_insufficient_funds", false)]
    public void Test_tokens_decide_the_provider_outcome(string token, bool approved)
    {
        Assert.Equal(approved, PaymentRules.Decide(token).Approved);
    }
}

public sealed class IdempotencyStoreTests
{
    [Fact]
    public async Task Concurrent_requests_with_the_same_key_start_exactly_once()
    {
        var store = new IdempotencyStore();
        using var start = new ManualResetEventSlim();
        var attempts = Enumerable.Range(0, 32).Select(_ => Task.Run(() =>
        {
            start.Wait();
            return store.TryBegin("order-1", "F").Decision;
        })).ToArray();

        start.Set();
        var decisions = await Task.WhenAll(attempts);

        Assert.Single(decisions, d => d == IdempotencyDecision.StartNew);
        Assert.All(decisions.Where(d => d != IdempotencyDecision.StartNew), d => Assert.Equal(IdempotencyDecision.RejectInProgress, d));
    }

    [Fact]
    public void Abandoned_request_can_start_again_and_completed_request_is_replayed()
    {
        var store = new IdempotencyStore();

        store.TryBegin("order-1", "F");
        store.Abandon("order-1");
        var restarted = store.TryBegin("order-1", "F").Decision;
        store.Complete("order-1", new StoredResponse(201, "{\"status\":\"authorized\"}"));
        var (replay, existing) = store.TryBegin("order-1", "F");

        Assert.Equal(IdempotencyDecision.StartNew, restarted);
        Assert.Equal(IdempotencyDecision.ReplayStored, replay);
        Assert.Equal(201, existing!.Response!.StatusCode);
    }
}
