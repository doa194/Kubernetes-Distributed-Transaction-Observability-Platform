using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Platform.TestSupport;

namespace Platform.IntegrationTests;

// A dependency that is completely gone (connection refused) must fail the order quickly with 503,
// not hang until the transaction budget runs out.
public sealed class DependencyUnavailableTests
{
    [Fact]
    public async Task Stopped_shipping_service_fails_the_order_with_503_within_budget()
    {
        await using var platform = new PlatformFixture();
        await platform.InitializeAsync();
        platform.ShippingHost.Dispose();

        var stopwatch = Stopwatch.StartNew();
        using var response = await platform.Orders.PostAsJsonAsync("/orders", TestOrders.Normal());
        var body = await response.Content.ReadFromJsonAsync<JsonElement>();

        // Linux refuses a connection to a closed port immediately (503, "unavailable"). Windows retries
        // the connection for about two seconds, so the one-second attempt timeout fires first (504).
        // Either way the order must fail as a technical shipping failure well within its budget.
        Assert.Contains(response.StatusCode, new[] { HttpStatusCode.ServiceUnavailable, HttpStatusCode.GatewayTimeout });
        Assert.Equal("ShippingFailed", body.GetProperty("state").GetString());
        Assert.NotEqual("business_rejection", body.GetProperty("failure").GetProperty("kind").GetString());
        Assert.True(stopwatch.Elapsed < TimeSpan.FromSeconds(12), $"took {stopwatch.Elapsed}");
    }
}
