namespace ServiceDefaults.Faults;

// In-memory list of active fault rules for this pod.
// Faults are per pod on purpose: the scenario runner configures each replica separately,
// which is what makes replica-specific degradation possible.
public sealed class FaultRuleStore(TimeProvider timeProvider)
{
    private readonly Lock gate = new();
    private readonly List<FaultRule> rules = [];

    public FaultRule Add(FaultRuleRequest request)
    {
        var now = timeProvider.GetUtcNow();
        var rule = new FaultRule
        {
            Id = Guid.NewGuid().ToString("N")[..12],
            Operation = request.Operation,
            Mode = request.Mode,
            DelayMs = request.DelayMs,
            StatusCode = request.StatusCode,
            Phase = request.Phase ?? FaultPhase.BeforeEffect,
            FailAttempts = request.FailAttempts,
            EveryNth = request.EveryNth,
            ScenarioRunId = request.ScenarioRunId,
            MaxActivations = request.MaxActivations,
            CreatedAt = now,
            ExpiresAt = now.AddSeconds(request.TtlSeconds),
        };

        lock (gate)
        {
            rules.Add(rule);
        }

        return rule;
    }

    public IReadOnlyList<FaultRule> ListActive()
    {
        var now = timeProvider.GetUtcNow();
        lock (gate)
        {
            rules.RemoveAll(rule => rule.IsExpired(now));
            return [.. rules];
        }
    }

    public int Clear()
    {
        lock (gate)
        {
            var count = rules.Count;
            rules.Clear();
            return count;
        }
    }

    public bool Remove(string id)
    {
        lock (gate)
        {
            return rules.RemoveAll(rule => rule.Id == id) > 0;
        }
    }

    // Returns the oldest rule that applies to this call, or null.
    public FaultRule? FindMatch(string operation, FaultPhase phase, string? scenarioRunId)
    {
        var now = timeProvider.GetUtcNow();
        lock (gate)
        {
            return rules.FirstOrDefault(rule =>
                rule.Operation == operation
                && rule.Mode != FaultMode.ReadinessLoss
                && rule.Phase == phase
                && !rule.IsExpired(now)
                && !rule.IsExhausted
                && (rule.ScenarioRunId is null || rule.ScenarioRunId == scenarioRunId));
        }
    }

    public bool HasActiveReadinessLoss()
    {
        var now = timeProvider.GetUtcNow();
        lock (gate)
        {
            return rules.Any(rule => rule.Mode == FaultMode.ReadinessLoss && !rule.IsExpired(now));
        }
    }
}
