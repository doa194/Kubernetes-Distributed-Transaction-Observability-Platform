// HTTP API of ShippingService: create the shipment of an order.
using System.Diagnostics;
using ServiceDefaults.Faults;
using ServiceDefaults.Telemetry;
using ShippingService.Domain;

namespace ShippingService.Features;

public sealed record CreateShipmentRequest(Guid OrderId, string? Zone, int ItemCount);

// Vertical slice of ShippingService: create the shipment for an order.
public static class ShipmentEndpoints
{
    public const string CreateOperation = "shipping.create";
    private const string ProblemTypeBase = "https://txplatform.local/problems/";

    public static IServiceCollection AddShippingFeatures(this IServiceCollection services) =>
        services.AddSingleton<ShipmentBook>();

    public static IEndpointRouteBuilder MapShipmentEndpoints(this IEndpointRouteBuilder endpoints)
    {
        endpoints.MapPost("/shipments", CreateAsync);
        return endpoints;
    }

    private static async Task<IResult> CreateAsync(CreateShipmentRequest request, ShipmentBook book, FaultInjector faults, CancellationToken cancellationToken)
    {
        var activity = Activity.Current;
        activity?.SetTag(TelemetryConventions.OrderId, request.OrderId.ToString());
        if (request.OrderId == Guid.Empty || string.IsNullOrWhiteSpace(request.Zone) || request.ItemCount < 1)
        {
            return Results.ValidationProblem(new Dictionary<string, string[]> { ["shipment"] = ["orderId, zone and a positive itemCount are required"] });
        }

        var fault = await faults.ApplyAsync(CreateOperation, FaultPhase.BeforeEffect, request.OrderId.ToString(), cancellationToken);
        if (fault.Outcome == FaultOutcome.Fail)
        {
            return Results.Problem(statusCode: fault.StatusCode, title: "Injected fault", type: ProblemTypeBase + "injected-fault");
        }

        var (outcome, shipment) = fault.Outcome == FaultOutcome.Reject
            ? (ShipmentOutcome.ZoneNotServiceable, null)
            : book.Create(request.OrderId, request.Zone, request.ItemCount);
        activity?.SetTag("shipping.outcome", outcome.ToString());

        await faults.ApplyAsync(CreateOperation, FaultPhase.AfterEffect, request.OrderId.ToString(), cancellationToken);
        return outcome switch
        {
            ShipmentOutcome.Created => Results.Created($"/shipments/{shipment!.ShipmentId}", new { shipmentId = shipment.ShipmentId, orderId = request.OrderId, status = "created" }),
            ShipmentOutcome.AlreadyCreated => Results.Ok(new { shipmentId = shipment!.ShipmentId, orderId = request.OrderId, status = "created" }),
            _ => Results.Problem(statusCode: StatusCodes.Status422UnprocessableEntity, title: "Zone not serviceable", type: ProblemTypeBase + "zone-not-serviceable", detail: request.Zone),
        };
    }
}
