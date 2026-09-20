using System.Net.Http.Json;
using OrderService.Features.CreateOrder;

namespace Platform.TestSupport;

// Order requests that produce each business outcome using plain data (no faults needed).
public static class TestOrders
{
    public static CreateOrderRequest Normal() =>
        new([new CreateOrderLine("SKU-1001", 2), new CreateOrderLine("SKU-1002", 1)], "tok_test_approved", "domestic");

    // 6 x 950.00 exceeds FraudService's 5000.00 limit.
    public static CreateOrderRequest HighRisk() =>
        new([new CreateOrderLine("SKU-2001", 6)], "tok_test_approved", "domestic");

    public static CreateOrderRequest Declined() =>
        new([new CreateOrderLine("SKU-1001", 1)], "tok_test_declined", "domestic");

    public static CreateOrderRequest OutOfStock() =>
        new([new CreateOrderLine("SKU-9000", 1)], "tok_test_approved", "domestic");

    public static CreateOrderRequest RestrictedZone() =>
        new([new CreateOrderLine("SKU-1001", 1)], "tok_test_approved", "restricted");
}

// Sets and clears fault rules through a service's management API.
public static class Faults
{
    public static async Task SetAsync(HttpClient client, object rule)
    {
        using var response = await client.PutAsJsonAsync("/internal/faults", rule);
        response.EnsureSuccessStatusCode();
    }

    public static async Task ClearAsync(HttpClient client)
    {
        using var response = await client.DeleteAsync("/internal/faults");
        response.EnsureSuccessStatusCode();
    }
}
