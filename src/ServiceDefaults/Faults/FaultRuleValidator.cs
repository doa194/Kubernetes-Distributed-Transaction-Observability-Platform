namespace ServiceDefaults.Faults;

// Validates operator-supplied fault rules before they can affect traffic.
// Rules must name an operation the service actually has, must expire, and must carry the
// parameters their mode needs. Invalid rules are rejected instead of silently doing nothing.
public static class FaultRuleValidator
{
    public const string ReadinessOperation = "readiness";
    public const int MaxDelayMs = 120_000;

    public static IReadOnlyDictionary<string, string[]> Validate(FaultRuleRequest request, IReadOnlyCollection<string> knownOperations, TimeSpan maxTtl)
    {
        var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);

        void Add(string field, string message) => errors[field] = [message];

        if (request.TtlSeconds < 1 || request.TtlSeconds > maxTtl.TotalSeconds)
        {
            Add("ttlSeconds", $"must be between 1 and {maxTtl.TotalSeconds:0} seconds so every fault expires");
        }

        if (request.Mode == FaultMode.ReadinessLoss)
        {
            if (request.Operation != ReadinessOperation)
            {
                Add("operation", $"readiness-loss faults must use the '{ReadinessOperation}' operation");
            }

            return errors;
        }

        if (!knownOperations.Contains(request.Operation))
        {
            Add("operation", $"unknown operation; known operations: {string.Join(", ", knownOperations)}");
        }

        if (request.DelayMs is < 1 or > MaxDelayMs)
        {
            Add("delayMs", $"must be between 1 and {MaxDelayMs}");
        }

        if (request.StatusCode is < 400 or > 599)
        {
            Add("statusCode", "must be an HTTP error status between 400 and 599");
        }

        switch (request.Mode)
        {
            case FaultMode.Latency when request.DelayMs is null:
                Add("delayMs", "is required for latency faults");
                break;
            case FaultMode.HttpError when request.StatusCode is null:
                Add("statusCode", "is required for http-error faults");
                break;
            case FaultMode.Intermittent:
                if (request.StatusCode is null)
                {
                    Add("statusCode", "is required for intermittent faults");
                }

                if ((request.FailAttempts is null) == (request.EveryNth is null))
                {
                    Add("failAttempts", "set exactly one of failAttempts or everyNth");
                }
                else if (request.FailAttempts is < 1 || request.EveryNth is < 2)
                {
                    Add("failAttempts", "failAttempts must be at least 1 and everyNth at least 2");
                }

                break;
        }

        if (request.Phase == FaultPhase.AfterEffect && request.Mode != FaultMode.Latency)
        {
            Add("phase", "only latency faults can run after the business effect");
        }

        if (request.MaxActivations is < 1)
        {
            Add("maxActivations", "must be at least 1");
        }

        return errors;
    }
}
