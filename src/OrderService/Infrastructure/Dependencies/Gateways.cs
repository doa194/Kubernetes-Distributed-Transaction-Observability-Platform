using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using OrderService.Domain;
using OrderService.Infrastructure.Telemetry;
using Polly.CircuitBreaker;
using Polly.Timeout;

namespace OrderService.Infrastructure.Dependencies;

// Adapter pattern: each gateway hides one downstream HTTP API behind business-level methods,
// so the coordinator deals only in StageResult values and never in status codes or exceptions.
public interface IInventoryGateway
{
    Task<StageResult> ReserveAsync(Order order, CancellationToken cancellationToken);

    Task<StageResult> ReleaseAsync(Guid orderId);
}

public interface IFraudGateway
{
    Task<StageResult> EvaluateAsync(Order order, CancellationToken cancellationToken);
}

public interface IPaymentGateway
{
    Task<StageResult> AuthorizeAsync(Order order, CancellationToken cancellationToken);

    Task<StageResult> VoidAsync(Guid orderId);
}

public interface IShippingGateway
{
    Task<StageResult> CreateAsync(Order order, CancellationToken cancellationToken);
}

public sealed class DependencyOptions
{
    public const string SectionName = "Dependencies";

    public Uri InventoryBaseUrl { get; set; } = new("http://inventory-service:8080");

    public Uri FraudBaseUrl { get; set; } = new("http://fraud-service:8080");

    public Uri PaymentBaseUrl { get; set; } = new("http://payment-service:8080");

    public Uri ShippingBaseUrl { get; set; } = new("http://shipping-service:8080");
}

internal sealed record ProblemBody(string? Type, string? Title, string? Detail);

// Shared translation of HTTP results and resilience exceptions into StageResult values.
internal static class DownstreamCall
{
    public static async Task<StageResult> ExecuteAsync(string stageName, Guid orderId, Func<CancellationToken, Task<StageResult>> call, CancellationToken cancellationToken)
    {
        using var stage = OrderTelemetry.StartStage(stageName, orderId);
        var result = await TranslateExceptionsAsync(call, cancellationToken);
        OrderTelemetry.RecordStageResult(stage, result);
        return result;
    }

    private static async Task<StageResult> TranslateExceptionsAsync(Func<CancellationToken, Task<StageResult>> call, CancellationToken cancellationToken)
    {
        try
        {
            return await call(cancellationToken);
        }
        catch (TimeoutRejectedException)
        {
            return StageResult.Failure(FailureKind.Timeout, "timeout");
        }
        catch (BrokenCircuitException)
        {
            return StageResult.Failure(FailureKind.Unavailable, "circuit_open");
        }
        catch (HttpRequestException error)
        {
            return StageResult.Failure(FailureKind.Unavailable, error.HttpRequestError.ToString().ToLowerInvariant());
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            // The whole transaction ran out of time while this stage was still waiting.
            return StageResult.Failure(FailureKind.Timeout, "transaction_budget_exceeded");
        }
    }

    public static async Task<StageResult> InterpretAsync(HttpResponseMessage response, CancellationToken cancellationToken)
    {
        if (response.IsSuccessStatusCode)
        {
            return StageResult.Success();
        }

        if (response.StatusCode == HttpStatusCode.UnprocessableEntity)
        {
            var problem = await response.Content.ReadFromJsonAsync<ProblemBody>(cancellationToken);
            return StageResult.Failure(FailureKind.BusinessRejection, ReasonFromProblemType(problem?.Type));
        }

        return StageResult.Failure(FailureKind.DependencyError, $"http_{(int)response.StatusCode}");
    }

    // Problem types are URIs such as https://txplatform.local/problems/insufficient-stock.
    public static string ReasonFromProblemType(string? type) =>
        string.IsNullOrWhiteSpace(type) ? "rejected" : type.TrimEnd('/').Split('/')[^1];
}

internal sealed class InventoryGateway(IHttpClientFactory clients) : IInventoryGateway
{
    public Task<StageResult> ReserveAsync(Order order, CancellationToken cancellationToken) =>
        DownstreamCall.ExecuteAsync("inventory.reserve", order.Id, async ct =>
        {
            var body = new { orderId = order.Id, lines = order.Lines.Select(l => new { sku = l.Sku, quantity = l.Quantity }) };
            using var response = await clients.CreateClient(DependencyProfiles.Inventory.Name).PostAsJsonAsync("/inventory/reservations", body, ct);
            return await DownstreamCall.InterpretAsync(response, ct);
        }, cancellationToken);

    public Task<StageResult> ReleaseAsync(Guid orderId) =>
        DownstreamCall.ExecuteAsync("compensation.inventory.release", orderId, async ct =>
        {
            using var response = await clients.CreateClient(DependencyProfiles.InventoryCompensation.Name).DeleteAsync($"/inventory/reservations/{orderId}", ct);
            // Nothing to release is a successful undo: the reservation never existed.
            return response.StatusCode == HttpStatusCode.NotFound ? StageResult.Success() : await DownstreamCall.InterpretAsync(response, ct);
        }, CancellationToken.None);
}

internal sealed class FraudGateway(IHttpClientFactory clients) : IFraudGateway
{
    private sealed record Evaluation(string Decision, string? Rule);

    public Task<StageResult> EvaluateAsync(Order order, CancellationToken cancellationToken) =>
        DownstreamCall.ExecuteAsync("fraud.evaluate", order.Id, async ct =>
        {
            var body = new { orderId = order.Id, totalAmount = order.TotalAmount, itemCount = order.ItemCount };
            using var response = await clients.CreateClient(DependencyProfiles.Fraud.Name).PostAsJsonAsync("/fraud/evaluations", body, ct);
            if (!response.IsSuccessStatusCode)
            {
                return await DownstreamCall.InterpretAsync(response, ct);
            }

            var evaluation = await response.Content.ReadFromJsonAsync<Evaluation>(ct);
            Activity.Current?.SetTag("fraud.decision", evaluation?.Decision);
            return evaluation?.Decision == "approved"
                ? StageResult.Success()
                : StageResult.Failure(FailureKind.BusinessRejection, evaluation?.Rule ?? "fraud_rejected");
        }, cancellationToken);
}

internal sealed class PaymentGateway(IHttpClientFactory clients) : IPaymentGateway
{
    public static string IdempotencyKeyFor(Guid orderId) => $"order-{orderId:N}-authorize";

    public Task<StageResult> AuthorizeAsync(Order order, CancellationToken cancellationToken) =>
        DownstreamCall.ExecuteAsync("payment.authorize", order.Id, async ct =>
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, "/payments/authorizations")
            {
                Content = JsonContent.Create(new { orderId = order.Id, amount = order.TotalAmount, currency = order.Currency, paymentToken = order.PaymentToken }),
            };
            // The same key on every attempt lets PaymentService recognise retries and never authorize twice.
            request.Headers.Add("Idempotency-Key", IdempotencyKeyFor(order.Id));
            using var response = await clients.CreateClient(DependencyProfiles.Payment.Name).SendAsync(request, ct);
            return await DownstreamCall.InterpretAsync(response, ct);
        }, cancellationToken);

    public Task<StageResult> VoidAsync(Guid orderId) =>
        DownstreamCall.ExecuteAsync("compensation.payment.void", orderId, async ct =>
        {
            using var response = await clients.CreateClient(DependencyProfiles.PaymentCompensation.Name).PostAsJsonAsync("/payments/voids", new { orderId }, ct);
            return await DownstreamCall.InterpretAsync(response, ct);
        }, CancellationToken.None);
}

internal sealed class ShippingGateway(IHttpClientFactory clients) : IShippingGateway
{
    public Task<StageResult> CreateAsync(Order order, CancellationToken cancellationToken) =>
        DownstreamCall.ExecuteAsync("shipping.create", order.Id, async ct =>
        {
            var body = new { orderId = order.Id, zone = order.ShippingZone, itemCount = order.ItemCount };
            using var response = await clients.CreateClient(DependencyProfiles.Shipping.Name).PostAsJsonAsync("/shipments", body, ct);
            return await DownstreamCall.InterpretAsync(response, ct);
        }, cancellationToken);
}
