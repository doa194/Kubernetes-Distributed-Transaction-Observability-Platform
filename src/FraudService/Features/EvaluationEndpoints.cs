// HTTP API of FraudService: evaluate the risk of one order.
using System.Diagnostics;
using FraudService.Domain;
using Microsoft.Extensions.Options;
using ServiceDefaults.Faults;
using ServiceDefaults.Telemetry;

namespace FraudService.Features;

public sealed record EvaluateOrderRequest(Guid OrderId, decimal TotalAmount, int ItemCount);

public sealed record EvaluationResponse(Guid OrderId, string Decision, string Rule, int RiskScore);

// Vertical slice of FraudService: evaluate an order's risk. A rejection is a successful
// evaluation with decision "rejected", not an HTTP error.
public static class EvaluationEndpoints
{
    public const string EvaluateOperation = "fraud.evaluate";

    public static IServiceCollection AddFraudFeatures(this IServiceCollection services, IConfiguration configuration)
    {
        services.Configure<RiskOptions>(configuration.GetSection(RiskOptions.SectionName));
        return services;
    }

    public static IEndpointRouteBuilder MapEvaluationEndpoints(this IEndpointRouteBuilder endpoints)
    {
        endpoints.MapPost("/fraud/evaluations", EvaluateAsync);
        return endpoints;
    }

    private static async Task<IResult> EvaluateAsync(EvaluateOrderRequest request, IOptions<RiskOptions> options, FaultInjector faults, CancellationToken cancellationToken)
    {
        var activity = Activity.Current;
        activity?.SetTag(TelemetryConventions.OrderId, request.OrderId.ToString());

        var fault = await faults.ApplyAsync(EvaluateOperation, FaultPhase.BeforeEffect, request.OrderId.ToString(), cancellationToken);
        if (fault.Outcome == FaultOutcome.Fail)
        {
            return Results.Problem(statusCode: fault.StatusCode, title: "Injected fault", type: "https://txplatform.local/problems/injected-fault");
        }

        var decision = fault.Outcome == FaultOutcome.Reject
            ? new RiskDecision(false, "simulated_rejection", 99)
            : RiskRules.Evaluate(request.TotalAmount, request.ItemCount, options.Value);

        activity?.SetTag("fraud.decision", decision.Approved ? "approved" : "rejected");
        activity?.SetTag("fraud.rule", decision.Rule);
        activity?.SetTag("fraud.risk_score", decision.RiskScore);

        await faults.ApplyAsync(EvaluateOperation, FaultPhase.AfterEffect, request.OrderId.ToString(), cancellationToken);
        return Results.Ok(new EvaluationResponse(request.OrderId, decision.Approved ? "approved" : "rejected", decision.Rule, decision.RiskScore));
    }
}
