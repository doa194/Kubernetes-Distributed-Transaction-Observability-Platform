using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text;
using Platform.TestSupport;

namespace Platform.IntegrationTests;

// The trace must describe what really happened across the five services: one trace id, the
// expected parent/child structure, concurrent checks, visible retries and faults, propagated
// scenario ids, and no sensitive values anywhere.
[Collection(PlatformCollection.Name)]
public sealed class TracePropagationTests(PlatformFixture platform) : IAsyncLifetime
{
    public async ValueTask InitializeAsync() => await platform.ResetAsync();

    public async ValueTask DisposeAsync() => await platform.ResetAsync();

    private async Task<(HttpResponseMessage Response, IReadOnlyList<Activity> Spans)> PlaceTracedOrderAsync(object order, string path = "/orders", Action<HttpRequestMessage>? configure = null)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, path) { Content = JsonContent.Create(order) };
        configure?.Invoke(request);
        var response = await platform.Orders.SendAsync(request);

        // traceresponse: 00-<trace id>-<span id>-<flags>
        var traceId = ActivityTraceId.CreateFromString(response.Headers.GetValues("traceresponse").Single().Split('-')[1]);
        // The OrderService server span ends just after the response is sent; wait until it is exported.
        var deadline = DateTime.UtcNow.AddSeconds(10);
        IReadOnlyList<Activity> spans = [];
        while (DateTime.UtcNow < deadline)
        {
            spans = platform.Spans.ForTrace(traceId);
            if (spans.Any(s => s.Kind == ActivityKind.Server && platform.ServiceOf(s) == "order-service"))
            {
                break;
            }

            await Task.Delay(50);
        }

        return (response, spans);
    }

    private static Activity Single(IReadOnlyList<Activity> spans, string name) => Assert.Single(spans, s => s.DisplayName == name);

    private IEnumerable<Activity> ServerSpans(IReadOnlyList<Activity> spans, string service) =>
        spans.Where(s => s.Kind == ActivityKind.Server && platform.ServiceOf(s) == service);

    [Fact]
    public async Task Order_trace_links_all_services_in_the_expected_structure()
    {
        var (response, spans) = await PlaceTracedOrderAsync(TestOrders.Normal(), configure: request =>
        {
            request.Headers.Add("X-Scenario-Id", "normal-order");
            request.Headers.Add("X-Scenario-Run-Id", "run-trace-structure");
            request.Headers.Add("X-Correlation-ID", "corr-trace-structure");
        });

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        Assert.Equal("corr-trace-structure", response.Headers.GetValues("X-Correlation-ID").Single());

        var orderServer = Assert.Single(ServerSpans(spans, "order-service"));
        var transaction = Single(spans, "order.transaction");
        Assert.Equal(orderServer.SpanId, transaction.ParentSpanId);
        Assert.Equal("Completed", transaction.GetTagItem("order.state"));
        Assert.Equal(
            ["InventoryReserved", "PaymentAuthorized", "ShipmentCreated", "Completed"],
            transaction.Events.Where(e => e.Name == "order.state_changed").Select(e => e.Tags.Single(t => t.Key == "order.state.to").Value));

        foreach (var (stageName, service) in new[] { ("inventory.reserve", "inventory-service"), ("fraud.evaluate", "fraud-service"), ("payment.authorize", "payment-service"), ("shipping.create", "shipping-service") })
        {
            var stage = Single(spans, stageName);
            Assert.Equal(transaction.SpanId, stage.ParentSpanId);
            var client = Assert.Single(spans, s => s.Kind == ActivityKind.Client && s.ParentSpanId == stage.SpanId);
            Assert.Equal(1, client.GetTagItem("retry.attempt"));
            var server = Assert.Single(ServerSpans(spans, service));
            Assert.Equal(client.SpanId, server.ParentSpanId);
            // Allow-listed baggage from OrderService reached the downstream service.
            Assert.Equal("normal-order", server.GetTagItem("scenario.id"));
            Assert.Equal("run-trace-structure", server.GetTagItem("scenario.run_id"));
            Assert.Equal("corr-trace-structure", server.GetTagItem("correlation.id"));
        }

        var inventory = Single(spans, "inventory.reserve");
        var fraud = Single(spans, "fraud.evaluate");
        Assert.True(inventory.StartTimeUtc < fraud.StartTimeUtc + fraud.Duration && fraud.StartTimeUtc < inventory.StartTimeUtc + inventory.Duration, "inventory and fraud stages must overlap");
        Assert.True(Single(spans, "payment.authorize").StartTimeUtc >= inventory.StartTimeUtc + inventory.Duration, "payment must start after the parallel checks finished");
    }

    [Fact]
    public async Task Retried_payment_attempts_are_separate_spans_in_the_same_trace()
    {
        await Faults.SetAsync(platform.Payment, new { operation = "payment.authorize", mode = "intermittent", statusCode = 503, failAttempts = 1, ttlSeconds = 60 });

        var (response, spans) = await PlaceTracedOrderAsync(TestOrders.Normal());

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        var stage = Single(spans, "payment.authorize");
        var attempts = spans.Where(s => s.Kind == ActivityKind.Client && s.ParentSpanId == stage.SpanId).OrderBy(s => s.StartTimeUtc).ToList();
        Assert.Equal([1, 2], attempts.Select(a => (int)a.GetTagItem("retry.attempt")!));
        Assert.Equal(1, stage.GetTagItem("retry.count"));
        Assert.Contains(stage.Events, e => e.Name == "retry");
        Assert.Equal([1, 2], ServerSpans(spans, "payment-service").Select(s => (int)s.GetTagItem("payment.attempt")!).Order());
    }

    [Fact]
    public async Task Injected_fault_failure_state_and_compensation_are_visible()
    {
        await Faults.SetAsync(platform.Payment, new { operation = "payment.authorize", mode = "http-error", statusCode = 503, ttlSeconds = 60 });

        var (response, spans) = await PlaceTracedOrderAsync(TestOrders.Normal());

        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        var authorizationAttempts = ServerSpans(spans, "payment-service").Where(s => s.DisplayName.Contains("authorizations", StringComparison.Ordinal)).ToList();
        Assert.Equal(3, authorizationAttempts.Count);
        Assert.All(authorizationAttempts, server =>
        {
            Assert.Equal(true, server.GetTagItem("failure.injected"));
            Assert.Equal("http-error", server.GetTagItem("simulation.mode"));
        });
        // The compensating void call was not targeted by the fault and must not be marked as injected.
        Assert.Null(Assert.Single(ServerSpans(spans, "payment-service"), s => s.DisplayName.Contains("voids", StringComparison.Ordinal)).GetTagItem("failure.injected"));
        Assert.Equal(ActivityStatusCode.Error, Single(spans, "payment.authorize").Status);
        var transaction = Single(spans, "order.transaction");
        Assert.Equal(ActivityStatusCode.Error, transaction.Status);
        Assert.Equal("payment", transaction.GetTagItem("failure.stage"));
        Assert.Equal("PaymentFailed", transaction.GetTagItem("order.state"));
        Assert.Equal(2, transaction.Events.Count(e => e.Name == "compensation.completed"));
        Assert.Contains(spans, s => s.DisplayName == "compensation.payment.void");
        Assert.DoesNotContain(spans, s => s.DisplayName == "shipping.create");
    }

    [Fact]
    public async Task Business_rejection_is_not_marked_as_an_error()
    {
        var (response, spans) = await PlaceTracedOrderAsync(TestOrders.HighRisk());

        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
        var transaction = Single(spans, "order.transaction");
        Assert.NotEqual(ActivityStatusCode.Error, transaction.Status);
        Assert.Equal("business_rejection", transaction.GetTagItem("failure.kind"));
    }

    [Fact]
    public async Task Health_and_management_calls_create_no_spans()
    {
        var before = platform.Spans.Snapshot().Count;

        foreach (var client in new[] { platform.Orders, platform.Payment, platform.Inventory })
        {
            (await client.GetAsync("/health/live")).EnsureSuccessStatusCode();
            (await client.GetAsync("/health/ready")).EnsureSuccessStatusCode();
        }

        (await platform.Payment.GetAsync("/internal/faults")).EnsureSuccessStatusCode();
        await Task.Delay(200);

        Assert.DoesNotContain(platform.Spans.Snapshot().Skip(before), span =>
            span.GetTagItem("url.path") is string path && (path.StartsWith("/health", StringComparison.Ordinal) || path.StartsWith("/internal", StringComparison.Ordinal)));
    }

    [Fact]
    public async Task Sensitive_values_never_reach_any_span()
    {
        var order = new { items = new[] { new { sku = "SKU-1001", quantity = 1 } }, paymentToken = "tok_canary_payment_7781", shippingZone = "domestic" };
        var token = TestTokens.Create();

        var (response, spans) = await PlaceTracedOrderAsync(order, "/orders?access_token=canary-query-4410", request =>
        {
            request.Headers.Authorization = new("Bearer", token);
            request.Headers.Add("X-Api-Key", "canary-header-5520");
            // Baggage from outside is discarded at OrderService, so this value must not appear either.
            request.Headers.Add("baggage", "scenario.id=canary-baggage-6630");
        });

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        Assert.True(spans.Count >= 10);
        var everything = new StringBuilder();
        foreach (var span in spans)
        {
            everything.AppendJoin('\n', span.TagObjects.Select(t => $"{t.Key}={t.Value}"));
            everything.AppendJoin('\n', span.Events.SelectMany(e => e.Tags.Select(t => $"{e.Name}:{t.Key}={t.Value}")));
        }

        var telemetry = everything.ToString();
        foreach (var secret in new[] { "canary-query-4410", "canary-header-5520", "tok_canary_payment_7781", "canary-baggage-6630", token })
        {
            Assert.DoesNotContain(secret, telemetry);
        }
    }
}
