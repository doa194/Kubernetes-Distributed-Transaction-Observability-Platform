// Operator API for fault rules, and the readiness check a readiness fault influences.
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using Microsoft.Extensions.Options;

namespace ServiceDefaults.Faults;

public sealed class FaultOptions
{
    public const string SectionName = "Faults";

    // Off by default: only the local deployment values turn fault injection on.
    public bool Enabled { get; set; }

    public int MaxTtlSeconds { get; set; } = 3600;
}

// Operator API for fault rules. It lives under /internal, which port separation restricts to
// the management port, so it is unreachable through Kubernetes Services and the Kong gateway.
internal static class FaultEndpoints
{
    public static void Map(IEndpointRouteBuilder endpoints, IReadOnlyCollection<string> knownOperations)
    {
        var group = endpoints.MapGroup("/internal/faults");

        group.MapGet("/", (FaultRuleStore store) => TypedResults.Ok(store.ListActive()));

        group.MapPut("/", (FaultRuleRequest request, FaultRuleStore store, IOptions<FaultOptions> options) =>
        {
            var errors = FaultRuleValidator.Validate(request, knownOperations, TimeSpan.FromSeconds(options.Value.MaxTtlSeconds));
            if (errors.Count > 0)
            {
                return Results.ValidationProblem(errors, title: "Invalid fault rule");
            }

            var rule = store.Add(request);
            return Results.Created($"/internal/faults/{rule.Id}", rule);
        });

        group.MapDelete("/", (FaultRuleStore store) => TypedResults.Ok(new { removed = store.Clear() }));

        group.MapDelete("/{id}", (string id, FaultRuleStore store) =>
            store.Remove(id) ? Results.NoContent() : Results.NotFound());
    }
}

// Lets an operator make a pod unready without stopping it, to observe how Kubernetes
// removes it from Service endpoints while liveness stays healthy.
internal sealed class FaultReadinessCheck(FaultRuleStore store) : IHealthCheck
{
    public Task<HealthCheckResult> CheckHealthAsync(HealthCheckContext context, CancellationToken cancellationToken = default) =>
        Task.FromResult(store.HasActiveReadinessLoss()
            ? HealthCheckResult.Unhealthy("readiness loss injected by a fault rule")
            : HealthCheckResult.Healthy());
}
