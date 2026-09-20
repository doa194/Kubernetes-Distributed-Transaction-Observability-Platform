using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Time.Testing;
using ServiceDefaults.Faults;
using ServiceDefaults.Hosting;

namespace Platform.UnitTests.Defaults;

// Fault rules must be valid, expire, stay within their scope and behave deterministically,
// otherwise scenarios would not be repeatable or would leak into unrelated traffic.
public sealed class FaultRuleValidatorTests
{
    private static readonly string[] Operations = ["payment.authorize"];
    private static readonly TimeSpan MaxTtl = TimeSpan.FromHours(1);

    private static IReadOnlyDictionary<string, string[]> Validate(FaultRuleRequest request) => FaultRuleValidator.Validate(request, Operations, MaxTtl);

    [Fact]
    public void Complete_latency_rule_is_valid()
    {
        Assert.Empty(Validate(new FaultRuleRequest("payment.authorize", FaultMode.Latency, 60, DelayMs: 1200, Phase: FaultPhase.AfterEffect)));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(3601)]
    public void Rules_must_expire_within_the_maximum_lifetime(int ttl)
    {
        Assert.Contains("ttlSeconds", Validate(new FaultRuleRequest("payment.authorize", FaultMode.HttpError, ttl, StatusCode: 503)).Keys);
    }

    [Fact]
    public void Unknown_operation_and_missing_mode_parameters_are_rejected()
    {
        Assert.Contains("operation", Validate(new FaultRuleRequest("payment.refund", FaultMode.HttpError, 60, StatusCode: 503)).Keys);
        Assert.Contains("delayMs", Validate(new FaultRuleRequest("payment.authorize", FaultMode.Latency, 60)).Keys);
        Assert.Contains("statusCode", Validate(new FaultRuleRequest("payment.authorize", FaultMode.HttpError, 60)).Keys);
    }

    [Fact]
    public void Intermittent_rules_need_exactly_one_pattern()
    {
        Assert.Contains("failAttempts", Validate(new FaultRuleRequest("payment.authorize", FaultMode.Intermittent, 60, StatusCode: 503)).Keys);
        Assert.Contains("failAttempts", Validate(new FaultRuleRequest("payment.authorize", FaultMode.Intermittent, 60, StatusCode: 503, FailAttempts: 1, EveryNth: 2)).Keys);
        Assert.Empty(Validate(new FaultRuleRequest("payment.authorize", FaultMode.Intermittent, 60, StatusCode: 503, EveryNth: 3)));
    }

    [Fact]
    public void Only_latency_can_run_after_the_business_effect()
    {
        Assert.Contains("phase", Validate(new FaultRuleRequest("payment.authorize", FaultMode.HttpError, 60, StatusCode: 503, Phase: FaultPhase.AfterEffect)).Keys);
    }

    [Fact]
    public void Readiness_loss_uses_the_readiness_operation()
    {
        Assert.Empty(Validate(new FaultRuleRequest("readiness", FaultMode.ReadinessLoss, 60)));
        Assert.Contains("operation", Validate(new FaultRuleRequest("payment.authorize", FaultMode.ReadinessLoss, 60)).Keys);
    }
}

public sealed class FaultRuleStoreTests
{
    private readonly FakeTimeProvider time = new(new DateTimeOffset(2026, 9, 17, 10, 0, 0, TimeSpan.Zero));

    [Fact]
    public void Rules_stop_matching_when_they_expire()
    {
        var store = new FaultRuleStore(time);
        store.Add(new FaultRuleRequest("payment.authorize", FaultMode.HttpError, 30, StatusCode: 503));

        var before = store.FindMatch("payment.authorize", FaultPhase.BeforeEffect, null);
        time.Advance(TimeSpan.FromSeconds(30));

        Assert.NotNull(before);
        Assert.Null(store.FindMatch("payment.authorize", FaultPhase.BeforeEffect, null));
        Assert.Empty(store.ListActive());
    }

    [Fact]
    public void Scoped_rules_only_affect_their_scenario_run()
    {
        var store = new FaultRuleStore(time);
        store.Add(new FaultRuleRequest("payment.authorize", FaultMode.HttpError, 60, StatusCode: 503, ScenarioRunId: "run-a"));

        Assert.NotNull(store.FindMatch("payment.authorize", FaultPhase.BeforeEffect, "run-a"));
        Assert.Null(store.FindMatch("payment.authorize", FaultPhase.BeforeEffect, "run-b"));
        Assert.Null(store.FindMatch("payment.authorize", FaultPhase.BeforeEffect, null));
    }

    [Fact]
    public void Rules_match_only_their_phase_and_stop_after_max_activations()
    {
        var store = new FaultRuleStore(time);
        var rule = store.Add(new FaultRuleRequest("payment.authorize", FaultMode.Latency, 60, DelayMs: 100, Phase: FaultPhase.AfterEffect, MaxActivations: 1));

        Assert.Null(store.FindMatch("payment.authorize", FaultPhase.BeforeEffect, null));
        Assert.Same(rule, store.FindMatch("payment.authorize", FaultPhase.AfterEffect, null));
        rule.RecordActivation();
        Assert.Null(store.FindMatch("payment.authorize", FaultPhase.AfterEffect, null));
    }

    [Fact]
    public void Readiness_loss_is_reported_until_it_expires()
    {
        var store = new FaultRuleStore(time);
        store.Add(new FaultRuleRequest("readiness", FaultMode.ReadinessLoss, 10));

        Assert.True(store.HasActiveReadinessLoss());
        time.Advance(TimeSpan.FromSeconds(10));
        Assert.False(store.HasActiveReadinessLoss());
    }
}

public sealed class FaultStrategyTests
{
    private readonly FakeTimeProvider time = new(new DateTimeOffset(2026, 9, 17, 10, 0, 0, TimeSpan.Zero));

    private FaultRule Rule(FaultRuleRequest request) => new FaultRuleStore(time).Add(request);

    [Fact]
    public async Task Intermittent_first_attempts_fail_per_key_then_succeed()
    {
        var strategy = new IntermittentStrategy();
        var rule = Rule(new FaultRuleRequest("payment.authorize", FaultMode.Intermittent, 60, StatusCode: 503, FailAttempts: 2));

        var keyA = new[] { await strategy.ExecuteAsync(rule, "a", default), await strategy.ExecuteAsync(rule, "a", default), await strategy.ExecuteAsync(rule, "a", default) };
        var keyB = await strategy.ExecuteAsync(rule, "b", default);

        Assert.Equal([FaultOutcome.Fail, FaultOutcome.Fail, FaultOutcome.Continue], keyA.Select(d => d.Outcome));
        Assert.Equal(FaultOutcome.Fail, keyB.Outcome);
    }

    [Fact]
    public async Task Intermittent_every_nth_request_fails()
    {
        var strategy = new IntermittentStrategy();
        var rule = Rule(new FaultRuleRequest("payment.authorize", FaultMode.Intermittent, 60, StatusCode: 500, EveryNth: 3));

        var outcomes = new List<FaultOutcome>();
        for (var i = 0; i < 6; i++)
        {
            outcomes.Add((await strategy.ExecuteAsync(rule, null, default)).Outcome);
        }

        Assert.Equal([FaultOutcome.Continue, FaultOutcome.Continue, FaultOutcome.Fail, FaultOutcome.Continue, FaultOutcome.Continue, FaultOutcome.Fail], outcomes);
    }

    [Fact]
    public async Task Timeout_holds_until_the_caller_cancels()
    {
        var strategy = new TimeoutStrategy(time);
        var rule = Rule(new FaultRuleRequest("payment.authorize", FaultMode.Timeout, 60));
        using var caller = new CancellationTokenSource();

        var pending = strategy.ExecuteAsync(rule, null, caller.Token).AsTask();
        time.Advance(TimeSpan.FromSeconds(30));
        Assert.False(pending.IsCompleted);
        await caller.CancelAsync();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => pending);
    }

    [Fact]
    public async Task Latency_continues_after_the_delay()
    {
        var strategy = new LatencyStrategy(time);
        var rule = Rule(new FaultRuleRequest("payment.authorize", FaultMode.Latency, 60, DelayMs: 1200));

        var pending = strategy.ExecuteAsync(rule, null, default).AsTask();
        time.Advance(TimeSpan.FromMilliseconds(1199));
        Assert.False(pending.IsCompleted);
        time.Advance(TimeSpan.FromMilliseconds(1));

        Assert.Equal(FaultOutcome.Continue, (await pending).Outcome);
    }
}

public sealed class PortSeparationTests
{
    private static readonly PortOptions Separated = new() { Business = 8080, Management = 8081 };

    [Theory]
    [InlineData("/orders", 8080, true)]
    [InlineData("/orders", 8081, false)]
    [InlineData("/health/ready", 8081, true)]
    [InlineData("/health/ready", 8080, false)]
    [InlineData("/internal/faults", 8080, false)]
    [InlineData("/INTERNAL/faults", 8080, false)]
    public void Management_paths_are_only_served_on_the_management_port(string path, int localPort, bool allowed)
    {
        Assert.Equal(allowed, PortSeparation.IsAllowed(new PathString(path), localPort, Separated));
    }

    [Fact]
    public void Separation_can_be_disabled_for_in_memory_test_hosts()
    {
        Assert.True(PortSeparation.IsAllowed(new PathString("/internal/faults"), 0, new PortOptions { Business = 0, Management = 0 }));
    }
}
