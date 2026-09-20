using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Platform.TestSupport;

namespace Platform.IntegrationTests;

// End-to-end behavior of the order transaction across real HTTP boundaries: concurrency,
// timeouts, retries, idempotency and compensation.
[Collection(PlatformCollection.Name)]
public sealed class TransactionFlowTests(PlatformFixture platform) : IAsyncLifetime
{
    public async ValueTask InitializeAsync() => await platform.ResetAsync();

    public async ValueTask DisposeAsync() => await platform.ResetAsync();

    private async Task<(HttpResponseMessage Response, JsonElement Body)> PlaceAsync(object order)
    {
        var response = await platform.Orders.PostAsJsonAsync("/orders", order);
        return (response, await response.Content.ReadFromJsonAsync<JsonElement>());
    }

    private static Guid OrderId(JsonElement body) => body.GetProperty("orderId").GetGuid();

    private static string[] CompensationActions(JsonElement body) =>
        [.. body.GetProperty("compensation").EnumerateArray().Select(step => $"{step.GetProperty("action").GetString()}:{step.GetProperty("status").GetString()}")];

    [Fact]
    public async Task Completed_order_is_authorized_exactly_once()
    {
        var (response, body) = await PlaceAsync(TestOrders.Normal());

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        var ledger = await platform.LedgerAsync(OrderId(body));
        Assert.Equal(1, ledger.GetProperty("authorizations").GetInt32());
    }

    [Fact]
    public async Task Inventory_and_fraud_checks_run_concurrently()
    {
        await Faults.SetAsync(platform.Inventory, new { operation = "inventory.reserve", mode = "latency", delayMs = 500, ttlSeconds = 60 });
        await Faults.SetAsync(platform.Fraud, new { operation = "fraud.evaluate", mode = "latency", delayMs = 450, ttlSeconds = 60 });

        var stopwatch = Stopwatch.StartNew();
        var (response, _) = await PlaceAsync(TestOrders.Normal());
        stopwatch.Stop();

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        // Sequential calls would take at least 950 ms; concurrent calls take about as long as the slower one.
        Assert.InRange(stopwatch.ElapsedMilliseconds, 500, 900);
    }

    [Fact]
    public async Task Timed_out_payment_attempt_is_retried_and_charged_once()
    {
        await Faults.SetAsync(platform.Payment, new { operation = "payment.authorize", mode = "latency", delayMs = 2000, maxActivations = 1, ttlSeconds = 60 });

        var stopwatch = Stopwatch.StartNew();
        var (response, body) = await PlaceAsync(TestOrders.Normal());

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        Assert.True(stopwatch.ElapsedMilliseconds >= 1500, "the first attempt must have hit the 1.5 s attempt timeout");
        Assert.Equal(1, (await platform.LedgerAsync(OrderId(body))).GetProperty("authorizations").GetInt32());
    }

    [Fact]
    public async Task Lost_payment_response_is_replayed_on_retry_instead_of_charging_twice()
    {
        // The first attempt records the authorization, then its reply is delayed past the attempt timeout.
        await Faults.SetAsync(platform.Payment, new { operation = "payment.authorize", mode = "latency", delayMs = 2500, phase = "after-effect", maxActivations = 1, ttlSeconds = 60 });

        var (response, body) = await PlaceAsync(TestOrders.Normal());

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        Assert.Equal(1, (await platform.LedgerAsync(OrderId(body))).GetProperty("authorizations").GetInt32());
    }

    [Fact]
    public async Task Concurrent_duplicate_payment_requests_charge_once()
    {
        var orderId = Guid.NewGuid();

        var responses = await Task.WhenAll(Enumerable.Range(0, 12).Select(async _ =>
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, "/payments/authorizations")
            {
                Content = JsonContent.Create(new { orderId, amount = 10m, currency = "EUR", paymentToken = "tok_test_approved" }),
            };
            request.Headers.Add("Idempotency-Key", $"order-{orderId:N}-authorize");
            using var response = await platform.Payment.SendAsync(request);
            return response.StatusCode;
        }));

        Assert.All(responses, status => Assert.Contains(status, new[] { HttpStatusCode.Created, HttpStatusCode.Conflict }));
        Assert.Equal(1, (await platform.LedgerAsync(orderId)).GetProperty("authorizations").GetInt32());
    }

    [Fact]
    public async Task Payment_outage_fails_the_order_and_undoes_earlier_steps()
    {
        await Faults.SetAsync(platform.Payment, new { operation = "payment.authorize", mode = "http-error", statusCode = 503, ttlSeconds = 60 });

        var (response, body) = await PlaceAsync(TestOrders.Normal());

        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        Assert.Equal("PaymentFailed", body.GetProperty("state").GetString());
        Assert.Equal(["VoidPayment:Succeeded", "ReleaseInventory:Succeeded"], CompensationActions(body));
    }

    [Fact]
    public async Task Timed_out_payment_fails_with_504_and_is_voided()
    {
        await Faults.SetAsync(platform.Payment, new { operation = "payment.authorize", mode = "timeout", ttlSeconds = 60 });

        var (response, body) = await PlaceAsync(TestOrders.Normal());

        Assert.Equal(HttpStatusCode.GatewayTimeout, response.StatusCode);
        Assert.Equal("timeout", body.GetProperty("failure").GetProperty("kind").GetString());
        Assert.Equal(["VoidPayment:Succeeded", "ReleaseInventory:Succeeded"], CompensationActions(body));
    }

    [Fact]
    public async Task Shipping_failure_voids_the_authorized_payment()
    {
        await Faults.SetAsync(platform.Shipping, new { operation = "shipping.create", mode = "http-error", statusCode = 500, ttlSeconds = 60 });

        var (response, body) = await PlaceAsync(TestOrders.Normal());

        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        Assert.Equal("ShippingFailed", body.GetProperty("state").GetString());
        var ledger = await platform.LedgerAsync(OrderId(body));
        Assert.Equal(1, ledger.GetProperty("voided").GetInt32());
    }

    [Fact]
    public async Task Declined_payment_releases_stock_without_voiding()
    {
        var (response, body) = await PlaceAsync(TestOrders.Declined());

        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
        Assert.Equal("payment-declined", body.GetProperty("failure").GetProperty("reason").GetString());
        Assert.Equal(["ReleaseInventory:Succeeded"], CompensationActions(body));
    }

    [Fact]
    public async Task Business_rejections_from_real_services_carry_their_reason()
    {
        var (outOfStock, outOfStockBody) = await PlaceAsync(TestOrders.OutOfStock());
        var (highRisk, highRiskBody) = await PlaceAsync(TestOrders.HighRisk());
        var (restricted, restrictedBody) = await PlaceAsync(TestOrders.RestrictedZone());

        Assert.Equal((HttpStatusCode.UnprocessableEntity, "InventoryFailed", "insufficient-stock"),
            (outOfStock.StatusCode, outOfStockBody.GetProperty("state").GetString(), outOfStockBody.GetProperty("failure").GetProperty("reason").GetString()));
        Assert.Equal((HttpStatusCode.UnprocessableEntity, "FraudFailed", "amount_threshold"),
            (highRisk.StatusCode, highRiskBody.GetProperty("state").GetString(), highRiskBody.GetProperty("failure").GetProperty("reason").GetString()));
        Assert.Equal((HttpStatusCode.UnprocessableEntity, "ShippingFailed", "zone-not-serviceable"),
            (restricted.StatusCode, restrictedBody.GetProperty("state").GetString(), restrictedBody.GetProperty("failure").GetProperty("reason").GetString()));
        Assert.Equal(["ReleaseInventory:Succeeded"], CompensationActions(highRiskBody));
    }

    [Fact]
    public async Task Open_circuit_fails_orders_fast_until_the_operator_reset_closes_it()
    {
        // Ten failed calls (the breaker's minimum throughput) open the shipping circuit.
        await Faults.SetAsync(platform.Shipping, new { operation = "shipping.create", mode = "http-error", statusCode = 500, ttlSeconds = 60 });
        for (var order = 0; order < 10; order++)
        {
            var (failed, _) = await PlaceAsync(TestOrders.Normal());
            Assert.Equal(HttpStatusCode.BadGateway, failed.StatusCode);
        }

        await Faults.ClearAsync(platform.Shipping);
        var (whileOpen, whileOpenBody) = await PlaceAsync(TestOrders.Normal());
        using var reset = await platform.Orders.PostAsync("/internal/resilience/reset", null);
        var (afterReset, _) = await PlaceAsync(TestOrders.Normal());

        // The healthy dependency is not called while the circuit is open; the reset makes it usable at once.
        Assert.Equal((HttpStatusCode.ServiceUnavailable, "circuit_open"),
            (whileOpen.StatusCode, whileOpenBody.GetProperty("failure").GetProperty("reason").GetString()));
        Assert.Equal(HttpStatusCode.OK, reset.StatusCode);
        Assert.Equal(HttpStatusCode.Created, afterReset.StatusCode);
    }
}
