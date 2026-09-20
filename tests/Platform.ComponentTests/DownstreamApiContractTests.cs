using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Platform.TestSupport;

namespace Platform.ComponentTests;

// Contracts of the private services, as OrderService's adapters rely on them.
public sealed class PaymentApiContractTests : IDisposable
{
    private readonly ServiceHost<PaymentService.AssemblyMarker> host = new();

    private HttpClient Client => host.CreateClient();

    private static HttpRequestMessage Authorize(Guid orderId, string? key, decimal amount = 25m, string token = "tok_test_approved")
    {
        var request = new HttpRequestMessage(HttpMethod.Post, "/payments/authorizations")
        {
            Content = JsonContent.Create(new { orderId, amount, currency = "EUR", paymentToken = token }),
        };
        if (key is not null)
        {
            request.Headers.Add("Idempotency-Key", key);
        }

        return request;
    }

    [Fact]
    public async Task Missing_idempotency_key_is_rejected()
    {
        using var response = await Client.SendAsync(Authorize(Guid.NewGuid(), key: null));

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Fact]
    public async Task Retry_with_same_key_replays_the_first_authorization_without_charging_again()
    {
        var orderId = Guid.NewGuid();
        var client = Client;

        using var first = await client.SendAsync(Authorize(orderId, "key-1"));
        using var retry = await client.SendAsync(Authorize(orderId, "key-1"));

        Assert.Equal(HttpStatusCode.Created, first.StatusCode);
        Assert.Equal(HttpStatusCode.Created, retry.StatusCode);
        Assert.Equal("true", retry.Headers.GetValues("Idempotent-Replayed").Single());
        var firstBody = await first.Content.ReadFromJsonAsync<JsonElement>();
        var retryBody = await retry.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(firstBody.GetProperty("authorizationId").GetString(), retryBody.GetProperty("authorizationId").GetString());
        var ledger = await client.GetFromJsonAsync<JsonElement>($"/internal/payments/{orderId}");
        Assert.Equal(1, ledger.GetProperty("authorizations").GetInt32());
    }

    [Fact]
    public async Task Same_key_with_different_amount_is_refused()
    {
        var orderId = Guid.NewGuid();
        var client = Client;

        using var first = await client.SendAsync(Authorize(orderId, "key-2", amount: 25m));
        using var misuse = await client.SendAsync(Authorize(orderId, "key-2", amount: 99m));

        Assert.Equal(HttpStatusCode.UnprocessableEntity, misuse.StatusCode);
        Assert.EndsWith("idempotency-key-mismatch", (await misuse.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("type").GetString());
    }

    [Fact]
    public async Task Declined_token_returns_payment_declined_problem_and_void_reports_nothing_to_void()
    {
        var orderId = Guid.NewGuid();
        var client = Client;

        using var declined = await client.SendAsync(Authorize(orderId, "key-3", token: "tok_test_declined"));
        using var voided = await client.PostAsJsonAsync("/payments/voids", new { orderId });

        Assert.Equal(HttpStatusCode.UnprocessableEntity, declined.StatusCode);
        Assert.EndsWith("payment-declined", (await declined.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("type").GetString());
        Assert.Equal("not_found", (await voided.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("status").GetString());
    }

    public void Dispose() => host.Dispose();
}

public sealed class InventoryApiContractTests : IDisposable
{
    private readonly ServiceHost<InventoryService.AssemblyMarker> host = new();

    [Fact]
    public async Task Reserve_is_idempotent_and_release_can_be_repeated()
    {
        var client = host.CreateClient();
        var orderId = Guid.NewGuid();
        var body = new { orderId, lines = new[] { new { sku = "SKU-1001", quantity = 2 } } };

        using var created = await client.PostAsJsonAsync("/inventory/reservations", body);
        using var repeated = await client.PostAsJsonAsync("/inventory/reservations", body);
        using var released = await client.DeleteAsync($"/inventory/reservations/{orderId}");
        using var unknown = await client.DeleteAsync($"/inventory/reservations/{Guid.NewGuid()}");

        Assert.Equal(HttpStatusCode.Created, created.StatusCode);
        Assert.Equal(HttpStatusCode.OK, repeated.StatusCode);
        Assert.Equal(HttpStatusCode.OK, released.StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, unknown.StatusCode);
    }

    [Fact]
    public async Task Out_of_stock_returns_insufficient_stock_problem()
    {
        using var response = await host.CreateClient().PostAsJsonAsync("/inventory/reservations",
            new { orderId = Guid.NewGuid(), lines = new[] { new { sku = "SKU-9000", quantity = 1 } } });

        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
        Assert.EndsWith("insufficient-stock", (await response.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("type").GetString());
    }

    public void Dispose() => host.Dispose();
}

public sealed class FraudAndShippingApiContractTests : IDisposable
{
    private readonly ServiceHost<FraudService.AssemblyMarker> fraud = new();
    private readonly ServiceHost<ShippingService.AssemblyMarker> shipping = new();

    [Theory]
    [InlineData(120.00, "approved")]
    [InlineData(5700.00, "rejected")]
    public async Task Fraud_rejection_is_a_successful_evaluation_with_a_decision(decimal total, string decision)
    {
        using var response = await fraud.CreateClient().PostAsJsonAsync("/fraud/evaluations", new { orderId = Guid.NewGuid(), totalAmount = total, itemCount = 2 });

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal(decision, (await response.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("decision").GetString());
    }

    [Fact]
    public async Task Shipment_creation_is_idempotent_and_restricted_zone_is_rejected()
    {
        var client = shipping.CreateClient();
        var orderId = Guid.NewGuid();

        using var created = await client.PostAsJsonAsync("/shipments", new { orderId, zone = "domestic", itemCount = 1 });
        using var repeated = await client.PostAsJsonAsync("/shipments", new { orderId, zone = "domestic", itemCount = 1 });
        using var restricted = await client.PostAsJsonAsync("/shipments", new { orderId = Guid.NewGuid(), zone = "restricted", itemCount = 1 });

        Assert.Equal(HttpStatusCode.Created, created.StatusCode);
        Assert.Equal(HttpStatusCode.OK, repeated.StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity, restricted.StatusCode);
    }

    public void Dispose()
    {
        fraud.Dispose();
        shipping.Dispose();
    }
}
