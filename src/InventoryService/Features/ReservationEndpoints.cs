// HTTP API of InventoryService: reserve and release stock for an order.
using System.Diagnostics;
using InventoryService.Domain;
using Microsoft.Extensions.Options;
using ServiceDefaults.Faults;
using ServiceDefaults.Telemetry;

namespace InventoryService.Features;

public sealed record ReserveStockRequest(Guid OrderId, IReadOnlyList<ReservationLine>? Lines);

// Vertical slices of InventoryService: reserve stock for an order and release it again.
public static class ReservationEndpoints
{
    public const string ReserveOperation = "inventory.reserve";
    public const string ReleaseOperation = "inventory.release";
    private const string ProblemTypeBase = "https://txplatform.local/problems/";

    public static IServiceCollection AddInventoryFeatures(this IServiceCollection services, IConfiguration configuration)
    {
        services.Configure<StockOptions>(configuration.GetSection(StockOptions.SectionName));
        services.AddSingleton(provider => new StockLedger(provider.GetRequiredService<IOptions<StockOptions>>().Value));
        return services;
    }

    public static IEndpointRouteBuilder MapReservationEndpoints(this IEndpointRouteBuilder endpoints)
    {
        endpoints.MapPost("/inventory/reservations", ReserveAsync);
        endpoints.MapDelete("/inventory/reservations/{orderId:guid}", ReleaseAsync);
        return endpoints;
    }

    private static async Task<IResult> ReserveAsync(ReserveStockRequest request, StockLedger ledger, FaultInjector faults, CancellationToken cancellationToken)
    {
        Activity.Current?.SetTag(TelemetryConventions.OrderId, request.OrderId.ToString());
        if (request.OrderId == Guid.Empty || request.Lines is not { Count: > 0 } || request.Lines.Any(l => l.Quantity < 1))
        {
            return Results.ValidationProblem(new Dictionary<string, string[]> { ["lines"] = ["an order id and at least one positive line are required"] });
        }

        var fault = await faults.ApplyAsync(ReserveOperation, FaultPhase.BeforeEffect, request.OrderId.ToString(), cancellationToken);
        if (fault.Outcome == FaultOutcome.Fail)
        {
            return Results.Problem(statusCode: fault.StatusCode, title: "Injected fault", type: ProblemTypeBase + "injected-fault");
        }

        if (fault.Outcome == FaultOutcome.Reject)
        {
            return InsufficientStock("simulated");
        }

        var result = ledger.Reserve(request.OrderId, request.Lines);
        Activity.Current?.SetTag("inventory.reservation.outcome", result.Outcome.ToString());

        await faults.ApplyAsync(ReserveOperation, FaultPhase.AfterEffect, request.OrderId.ToString(), cancellationToken);
        return result.Outcome switch
        {
            ReserveOutcome.Reserved => Results.Created($"/inventory/reservations/{request.OrderId}", new { orderId = request.OrderId, status = "reserved" }),
            ReserveOutcome.AlreadyReserved => Results.Ok(new { orderId = request.OrderId, status = "reserved" }),
            ReserveOutcome.InsufficientStock => InsufficientStock(result.Sku!),
            _ => Results.Problem(statusCode: StatusCodes.Status422UnprocessableEntity, title: "Unknown SKU", type: ProblemTypeBase + "unknown-sku", detail: result.Sku),
        };
    }

    private static async Task<IResult> ReleaseAsync(Guid orderId, StockLedger ledger, FaultInjector faults, CancellationToken cancellationToken)
    {
        Activity.Current?.SetTag(TelemetryConventions.OrderId, orderId.ToString());
        var fault = await faults.ApplyAsync(ReleaseOperation, FaultPhase.BeforeEffect, orderId.ToString(), cancellationToken);
        if (fault.Outcome == FaultOutcome.Fail)
        {
            return Results.Problem(statusCode: fault.StatusCode, title: "Injected fault", type: ProblemTypeBase + "injected-fault");
        }

        var released = ledger.Release(orderId);
        return released is null
            ? Results.NotFound()
            : Results.Ok(new { orderId, status = "released" });
    }

    private static IResult InsufficientStock(string sku) =>
        Results.Problem(statusCode: StatusCodes.Status422UnprocessableEntity, title: "Insufficient stock", type: ProblemTypeBase + "insufficient-stock", detail: sku);
}
