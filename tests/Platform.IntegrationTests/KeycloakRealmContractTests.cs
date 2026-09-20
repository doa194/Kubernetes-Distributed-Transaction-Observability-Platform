using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Platform.TestSupport;
using Testcontainers.Keycloak;

namespace Platform.IntegrationTests;

// Realm contract: tokens issued by a real Keycloak running the repository's realm file must be
// accepted and authorized by OrderService's real token validation. This catches drift between the
// realm (audience, permission mapper, roles) and the API, without needing the Kubernetes cluster.
public sealed class KeycloakRealmContractTests : IAsyncLifetime
{
    private const string RunnerSecret = "runner-contract-secret";
    private const string ReaderSecret = "reader-contract-secret";

    private KeycloakContainer keycloak = null!;
    private ServiceHost<OrderService.AssemblyMarker> orderService = null!;
    private string realmUrl = string.Empty;

    public async ValueTask InitializeAsync()
    {
        keycloak = new KeycloakBuilder(RepositoryFiles.PinnedImage("keycloak"))
            .WithResourceMapping(new FileInfo(RepositoryFiles.RealmFile), new FileInfo("/opt/keycloak/data/import/transaction-platform-realm.json"))
            .WithEnvironment("SCENARIO_RUNNER_CLIENT_SECRET", RunnerSecret)
            .WithEnvironment("ORDER_READER_CLIENT_SECRET", ReaderSecret)
            .WithCommand("--import-realm")
            .Build();
        await keycloak.StartAsync();

        realmUrl = $"{keycloak.GetBaseAddress().TrimEnd('/')}/realms/transaction-platform";
        orderService = new ServiceHost<OrderService.AssemblyMarker>()
            .WithSetting("Auth:Issuer", realmUrl)
            .WithSetting("Auth:MetadataAddress", $"{realmUrl}/.well-known/openid-configuration")
            .WithSetting("Auth:RequireHttpsMetadata", "false")
            // No downstream services exist here; only the authorization decision matters.
            .WithSetting("Dependencies:InventoryBaseUrl", "http://127.0.0.1:1")
            .WithSetting("Dependencies:FraudBaseUrl", "http://127.0.0.1:1")
            .WithSetting("Dependencies:PaymentBaseUrl", "http://127.0.0.1:1")
            .WithSetting("Dependencies:ShippingBaseUrl", "http://127.0.0.1:1");
    }

    private async Task<string> TokenAsync(string clientId, string secret)
    {
        using var http = new HttpClient();
        using var response = await http.PostAsync($"{realmUrl}/protocol/openid-connect/token", new FormUrlEncodedContent(new Dictionary<string, string>
        {
            ["grant_type"] = "client_credentials",
            ["client_id"] = clientId,
            ["client_secret"] = secret,
        }));
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("access_token").GetString()!;
    }

    private static JsonElement Claims(string token)
    {
        var payload = token.Split('.')[1].Replace('-', '+').Replace('_', '/');
        payload = payload.PadRight(payload.Length + ((4 - payload.Length % 4) % 4), '=');
        return JsonDocument.Parse(Encoding.UTF8.GetString(Convert.FromBase64String(payload))).RootElement;
    }

    private async Task<HttpStatusCode> CallAsync(HttpMethod method, string path, string token)
    {
        using var request = new HttpRequestMessage(method, path)
        {
            Content = method == HttpMethod.Post ? JsonContent.Create(TestOrders.Normal()) : null,
        };
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        using var response = await orderService.CreateClient().SendAsync(request);
        return response.StatusCode;
    }

    [Fact]
    public async Task Scenario_runner_token_carries_audience_and_all_permissions_and_may_create_orders()
    {
        var token = await TokenAsync("scenario-runner", RunnerSecret);
        var claims = Claims(token);

        Assert.Contains("order-api", claims.GetProperty("aud").ValueKind == JsonValueKind.Array
            ? claims.GetProperty("aud").EnumerateArray().Select(a => a.GetString())
            : [claims.GetProperty("aud").GetString()]);
        Assert.Equal(["orders.read", "orders.write", "traces.debug"], claims.GetProperty("permissions").EnumerateArray().Select(p => p.GetString()).Order());
        // Past authorization the transaction fails because no dependencies run here, which is not 401/403.
        Assert.DoesNotContain(await CallAsync(HttpMethod.Post, "/orders", token), new[] { HttpStatusCode.Unauthorized, HttpStatusCode.Forbidden });
    }

    [Fact]
    public async Task Order_reader_token_may_read_but_not_create()
    {
        var token = await TokenAsync("order-reader", ReaderSecret);

        Assert.Equal(["orders.read"], Claims(token).GetProperty("permissions").EnumerateArray().Select(p => p.GetString()));
        Assert.Equal(HttpStatusCode.NotFound, await CallAsync(HttpMethod.Get, $"/orders/{Guid.NewGuid()}", token));
        Assert.Equal(HttpStatusCode.Forbidden, await CallAsync(HttpMethod.Post, "/orders", token));
    }

    public async ValueTask DisposeAsync()
    {
        orderService?.Dispose();
        if (keycloak is not null)
        {
            await keycloak.DisposeAsync();
        }
    }
}
