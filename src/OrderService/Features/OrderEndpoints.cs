// The public Order API: create an order, read one back, and reset circuit breakers.
using Microsoft.AspNetCore.Http.HttpResults;
using Microsoft.Extensions.Options;
using OrderService.Domain;
using OrderService.Features.CreateOrder;
using OrderService.Infrastructure;
using OrderService.Infrastructure.Auth;
using OrderService.Infrastructure.Dependencies;

namespace OrderService.Features;

public sealed record OrderLineResponse(string Sku, int Quantity, decimal UnitPrice, decimal LineTotal);

public sealed record OrderResponse(
    Guid OrderId,
    OrderState State,
    decimal TotalAmount,
    string Currency,
    string ShippingZone,
    IReadOnlyList<OrderLineResponse> Lines,
    OrderFailure? Failure,
    IReadOnlyList<CompensationStepResult> Compensation,
    IReadOnlyList<OrderState> StateHistory,
    DateTimeOffset CreatedAt,
    DateTimeOffset UpdatedAt)
{
    public static OrderResponse From(OrderSnapshot order) => new(
        order.Id,
        order.State,
        order.TotalAmount,
        order.Currency,
        order.ShippingZone,
        [.. order.Lines.Select(line => new OrderLineResponse(line.Sku, line.Quantity, line.UnitPrice, line.LineTotal))],
        order.Failure,
        order.Compensation,
        [OrderState.Pending, .. order.History.Select(transition => transition.To)],
        order.CreatedAt,
        order.UpdatedAt);
}

// Maps a failed transaction to an HTTP status. Business rejections are the client's concern (422);
// technical failures describe what went wrong downstream (502, 503, 504).
public static class OrderOutcomeMapping
{
    public static int StatusCodeFor(FailureKind kind) => kind switch
    {
        FailureKind.BusinessRejection => StatusCodes.Status422UnprocessableEntity,
        FailureKind.DependencyError => StatusCodes.Status502BadGateway,
        FailureKind.Unavailable => StatusCodes.Status503ServiceUnavailable,
        FailureKind.Timeout => StatusCodes.Status504GatewayTimeout,
        _ => StatusCodes.Status500InternalServerError,
    };
}

public static class OrderEndpoints
{
    public const string ProblemTypeBase = "https://txplatform.local/problems/";

    public static IServiceCollection AddOrderFeatures(this IServiceCollection services, IConfiguration configuration)
    {
        services.Configure<CatalogOptions>(configuration.GetSection(CatalogOptions.SectionName));
        services.Configure<TransactionOptions>(configuration.GetSection(TransactionOptions.SectionName));
        services.AddSingleton(provider => new PriceCatalog(provider.GetRequiredService<IOptions<CatalogOptions>>().Value));
        services.AddSingleton<InMemoryOrderRepository>();
        services.AddSingleton<OrderTransactionCoordinator>();
        services.AddDependencyGateways(configuration);
        return services;
    }

    public static IEndpointRouteBuilder MapOrderEndpoints(this IEndpointRouteBuilder endpoints)
    {
        endpoints.MapPost("/orders", CreateOrderAsync).WithName("CreateOrder").RequireAuthorization(Permissions.OrdersWrite);
        endpoints.MapGet("/orders/{orderId:guid}", GetOrder).WithName("GetOrder").RequireAuthorization(Permissions.OrdersRead);
        return endpoints;
    }

    public static IEndpointRouteBuilder MapResilienceManagement(this IEndpointRouteBuilder endpoints)
    {
        // Operators close all circuit breakers between experiments so one outage cannot affect the next.
        endpoints.MapPost("/internal/resilience/reset", async (CircuitBreakerControls controls, CancellationToken cancellationToken) =>
            TypedResults.Ok(new { closedBreakers = await controls.CloseAllAsync(cancellationToken) }));
        return endpoints;
    }

    private static async Task<IResult> CreateOrderAsync(
        CreateOrderRequest? request,
        HttpContext http,
        PriceCatalog catalog,
        OrderTransactionCoordinator coordinator,
        TimeProvider timeProvider)
    {
        var errors = CreateOrderValidator.Validate(request, catalog);
        if (errors.Count > 0)
        {
            return TypedResults.ValidationProblem(errors, title: "The order is invalid", type: ProblemTypeBase + "invalid-order");
        }

        var lines = request!.Items!.Select(item =>
        {
            catalog.TryGetPrice(item.Sku!, out var price);
            return new OrderLine(item.Sku!, item.Quantity, price);
        }).ToList();
        var order = new Order(Guid.CreateVersion7(), lines, catalog.Currency, request.PaymentToken!, request.ShippingZone!, timeProvider.GetUtcNow());

        var result = await coordinator.ExecuteAsync(order);
        var location = $"{http.Request.Scheme}://{http.Request.Host}{http.Request.PathBase}/orders/{result.Id}";

        if (result.State == OrderState.Completed)
        {
            return TypedResults.Created(location, OrderResponse.From(result));
        }

        var failure = result.Failure!;
        http.Response.Headers.Location = location;
        return TypedResults.Problem(
            statusCode: OrderOutcomeMapping.StatusCodeFor(failure.Kind),
            title: $"The order ended in state {result.State}",
            type: ProblemTypeBase + (failure.Kind == FailureKind.BusinessRejection ? "order-rejected" : "order-dependency-failure"),
            detail: $"{FailureNames.Stage(failure.Stage)} failed: {failure.Reason}",
            extensions: new Dictionary<string, object?>
            {
                ["orderId"] = result.Id,
                ["state"] = result.State,
                ["failure"] = failure,
                ["compensation"] = result.Compensation,
            });
    }

    private static Results<Ok<OrderResponse>, ProblemHttpResult> GetOrder(Guid orderId, InMemoryOrderRepository repository)
    {
        var order = repository.Find(orderId);
        return order is null
            ? TypedResults.Problem(statusCode: StatusCodes.Status404NotFound, title: "Order not found", type: ProblemTypeBase + "order-not-found")
            : TypedResults.Ok(OrderResponse.From(order));
    }
}
