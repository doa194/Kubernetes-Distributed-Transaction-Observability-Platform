using System.Diagnostics;
using OpenTelemetry;
using ServiceDefaults.Telemetry;

namespace ServiceDefaults.Faults;

// Entry point handlers call at the moments a fault may apply.
// It finds a matching rule, runs its strategy and records on the current span that the
// behavior was simulated, so traces never confuse injected faults with real ones.
public sealed class FaultInjector(FaultRuleStore store, IEnumerable<IFaultStrategy> strategies)
{
    private readonly Dictionary<FaultMode, IFaultStrategy> strategiesByMode = strategies.ToDictionary(s => s.Mode);

    public async ValueTask<FaultDecision> ApplyAsync(string operation, FaultPhase phase, string? key, CancellationToken cancellationToken)
    {
        // Rules scoped to a scenario run only affect requests that carry that run's id in baggage.
        var scenarioRunId = Baggage.GetBaggage(TelemetryConventions.ScenarioRunId);
        var rule = store.FindMatch(operation, phase, scenarioRunId);
        if (rule is null)
        {
            return FaultDecision.Continue;
        }

        rule.RecordActivation();
        var decision = await strategiesByMode[rule.Mode].ExecuteAsync(rule, key, cancellationToken);
        RecordOnSpan(rule, phase, decision);
        return decision;
    }

    private static void RecordOnSpan(FaultRule rule, FaultPhase phase, FaultDecision decision)
    {
        var activity = Activity.Current;
        if (activity is null)
        {
            return;
        }

        var injectedBehavior = decision.Outcome != FaultOutcome.Continue || rule.Mode == FaultMode.Latency;
        if (!injectedBehavior)
        {
            return;
        }

        var mode = FaultWireNames.Mode(rule.Mode);
        activity.SetTag(TelemetryConventions.FailureInjected, true);
        activity.SetTag(TelemetryConventions.SimulationMode, mode);
        activity.SetTag(TelemetryConventions.FaultRuleId, rule.Id);
        activity.AddEvent(new ActivityEvent(TelemetryConventions.EventFaultApplied, tags: new ActivityTagsCollection
        {
            ["fault.mode"] = mode,
            ["fault.phase"] = FaultWireNames.Phase(phase),
            ["fault.outcome"] = decision.Outcome.ToString().ToLowerInvariant(),
            ["fault.delay_ms"] = rule.DelayMs,
            ["fault.status_code"] = decision.StatusCode == 0 ? null : decision.StatusCode,
        }));
    }
}

public static class FaultWireNames
{
    public static string Mode(FaultMode mode) => mode switch
    {
        FaultMode.Latency => "latency",
        FaultMode.HttpError => "http-error",
        FaultMode.Timeout => "timeout",
        FaultMode.Intermittent => "intermittent",
        FaultMode.BusinessRejection => "business-rejection",
        FaultMode.ReadinessLoss => "readiness-loss",
        _ => mode.ToString(),
    };

    public static string Phase(FaultPhase phase) => phase == FaultPhase.AfterEffect ? "after-effect" : "before-effect";
}
