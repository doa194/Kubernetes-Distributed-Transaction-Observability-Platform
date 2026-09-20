using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Platform.TestSupport;

namespace Platform.ComponentTests;

// Health endpoints and the fault-injection API, which the platform and the scenario runner use.
public sealed class ManagementApiTests : IDisposable
{
    private readonly ServiceHost<ShippingService.AssemblyMarker> host = new();

    [Fact]
    public async Task Readiness_loss_fault_makes_the_pod_unready_but_keeps_it_alive()
    {
        var client = host.CreateClient();

        await Faults.SetAsync(client, new { operation = "readiness", mode = "readiness-loss", ttlSeconds = 60 });
        using var ready = await client.GetAsync("/health/ready");
        using var live = await client.GetAsync("/health/live");
        await Faults.ClearAsync(client);
        using var readyAgain = await client.GetAsync("/health/ready");

        Assert.Equal(HttpStatusCode.ServiceUnavailable, ready.StatusCode);
        Assert.Equal(HttpStatusCode.OK, live.StatusCode);
        Assert.Equal(HttpStatusCode.OK, readyAgain.StatusCode);
    }

    [Fact]
    public async Task Invalid_fault_rule_is_rejected_with_field_errors()
    {
        using var response = await host.CreateClient().PutAsJsonAsync("/internal/faults", new { operation = "shipping.teleport", mode = "http-error", ttlSeconds = 0 });

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        var errors = (await response.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("errors");
        Assert.True(errors.TryGetProperty("operation", out _));
        Assert.True(errors.TryGetProperty("ttlSeconds", out _));
    }

    [Fact]
    public async Task Injected_http_error_changes_the_response_until_cleared()
    {
        var client = host.CreateClient();
        var shipment = new { orderId = Guid.NewGuid(), zone = "domestic", itemCount = 1 };

        await Faults.SetAsync(client, new { operation = "shipping.create", mode = "http-error", statusCode = 503, ttlSeconds = 60 });
        using var failed = await client.PostAsJsonAsync("/shipments", shipment);
        await Faults.ClearAsync(client);
        using var succeeded = await client.PostAsJsonAsync("/shipments", shipment);

        Assert.Equal(HttpStatusCode.ServiceUnavailable, failed.StatusCode);
        Assert.Equal(HttpStatusCode.Created, succeeded.StatusCode);
    }

    [Fact]
    public async Task Fault_api_does_not_exist_when_fault_injection_is_disabled()
    {
        using var disabled = new ServiceHost<ShippingService.AssemblyMarker>().WithSetting("Faults:Enabled", "false");

        using var response = await disabled.CreateClient().GetAsync("/internal/faults");

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    public void Dispose() => host.Dispose();
}
