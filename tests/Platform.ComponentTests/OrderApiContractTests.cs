using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using OrderService;
using OrderService.Domain;
using OrderService.Infrastructure.Dependencies;
using Platform.TestSupport;

namespace Platform.ComponentTests;

// The public Order API contract: status codes, problem details and Location headers for every
// outcome. Dependencies are replaced by stubs so each outcome can be forced precisely.
public sealed class OrderApiContractTests : IDisposable
{
    private readonly StubGateways stubs = new();
    private readonly ServiceHost<AssemblyMarker> host;
    private readonly HttpClient client;

    public OrderApiContractTests()
    {
        host = new ServiceHost<AssemblyMarker>().UseTestAuthentication().WithServices(stubs.Register);
        client = host.CreateClient();
        client.DefaultRequestHeaders.Authorization = new("Bearer", TestTokens.Create());
    }

    [Fact]
    public async Task Completed_order_returns_201_with_location_and_full_state_history()
    {
        using var response = await client.PostAsJsonAsync("/orders", TestOrders.Normal());

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        var body = await response.Content.ReadFromJsonAsync<JsonElement>();
        var orderId = body.GetProperty("orderId").GetGuid();
        Assert.Equal($"http://localhost/orders/{orderId}", response.Headers.Location!.ToString());
        Assert.Equal("Completed", body.GetProperty("state").GetString());
        Assert.Equal(74.90m, body.GetProperty("totalAmount").GetDecimal());
        Assert.Equal(
            ["Pending", "InventoryReserved", "PaymentAuthorized", "ShipmentCreated", "Completed"],
            body.GetProperty("stateHistory").EnumerateArray().Select(s => s.GetString()));
        Assert.False(body.TryGetProperty("paymentToken", out _));
    }

    [Fact]
    public async Task Invalid_order_returns_400_problem_and_calls_no_dependency()
    {
        using var response = await client.PostAsJsonAsync("/orders", new { items = Array.Empty<object>(), paymentToken = "4111111111111111", shippingZone = "moon" });

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        var problem = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.True(problem.GetProperty("errors").TryGetProperty("paymentToken", out _));
        Assert.Empty(stubs.Calls);
    }

    [Fact]
    public async Task Business_rejection_returns_422_with_order_reference_and_stored_state()
    {
        stubs.Fraud = StageResult.Failure(FailureKind.BusinessRejection, "amount_threshold");

        using var response = await client.PostAsJsonAsync("/orders", TestOrders.HighRisk());

        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
        var problem = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("FraudFailed", problem.GetProperty("state").GetString());
        Assert.Equal("business_rejection", problem.GetProperty("failure").GetProperty("kind").GetString());
        var stored = await client.GetFromJsonAsync<JsonElement>(response.Headers.Location);
        Assert.Equal("FraudFailed", stored.GetProperty("state").GetString());
        Assert.Contains("inventory.release", stubs.Calls);
    }

    [Theory]
    [InlineData(FailureKind.DependencyError, HttpStatusCode.BadGateway)]
    [InlineData(FailureKind.Unavailable, HttpStatusCode.ServiceUnavailable)]
    [InlineData(FailureKind.Timeout, HttpStatusCode.GatewayTimeout)]
    public async Task Technical_payment_failure_maps_to_gateway_status_and_undoes_earlier_steps(FailureKind kind, HttpStatusCode expected)
    {
        stubs.Payment = StageResult.Failure(kind, "simulated");

        using var response = await client.PostAsJsonAsync("/orders", TestOrders.Normal());

        Assert.Equal(expected, response.StatusCode);
        var problem = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("PaymentFailed", problem.GetProperty("state").GetString());
        Assert.Equal(["VoidPayment", "ReleaseInventory"], problem.GetProperty("compensation").EnumerateArray().Select(s => s.GetProperty("action").GetString()));
        Assert.DoesNotContain("shipping.create", stubs.Calls);
    }

    [Fact]
    public async Task Unknown_order_returns_404_problem()
    {
        using var response = await client.GetAsync($"/orders/{Guid.NewGuid()}");

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
        Assert.Equal("application/problem+json", response.Content.Headers.ContentType!.MediaType);
    }

    public void Dispose()
    {
        client.Dispose();
        host.Dispose();
    }

    private sealed class StubGateways : IInventoryGateway, IFraudGateway, IPaymentGateway, IShippingGateway
    {
        public StageResult Inventory { get; set; } = StageResult.Success();

        public StageResult Fraud { get; set; } = StageResult.Success();

        public StageResult Payment { get; set; } = StageResult.Success();

        public StageResult Shipping { get; set; } = StageResult.Success();

        public List<string> Calls { get; } = [];

        public void Register(IServiceCollection services)
        {
            services.AddSingleton<IInventoryGateway>(this);
            services.AddSingleton<IFraudGateway>(this);
            services.AddSingleton<IPaymentGateway>(this);
            services.AddSingleton<IShippingGateway>(this);
        }

        public Task<StageResult> ReserveAsync(Order order, CancellationToken cancellationToken) => Record("inventory.reserve", Inventory);

        public Task<StageResult> ReleaseAsync(Guid orderId) => Record("inventory.release", StageResult.Success());

        public Task<StageResult> EvaluateAsync(Order order, CancellationToken cancellationToken) => Record("fraud.evaluate", Fraud);

        public Task<StageResult> AuthorizeAsync(Order order, CancellationToken cancellationToken) => Record("payment.authorize", Payment);

        public Task<StageResult> VoidAsync(Guid orderId) => Record("payment.void", StageResult.Success());

        public Task<StageResult> CreateAsync(Order order, CancellationToken cancellationToken) => Record("shipping.create", Shipping);

        private Task<StageResult> Record(string call, StageResult result)
        {
            lock (Calls)
            {
                Calls.Add(call);
            }

            return Task.FromResult(result);
        }
    }
}
