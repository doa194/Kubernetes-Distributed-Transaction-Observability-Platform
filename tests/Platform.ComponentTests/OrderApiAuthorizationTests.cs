using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using Microsoft.IdentityModel.Tokens;
using OrderService;
using Platform.TestSupport;

namespace Platform.ComponentTests;

// Authentication and authorization matrix of the public Order API. Tokens are created with a
// test key, which makes every invalid-token variant cheap and deterministic to produce.
public sealed class OrderApiAuthorizationTests : IDisposable
{
    private readonly ServiceHost<AssemblyMarker> host = new ServiceHost<AssemblyMarker>().UseTestAuthentication();

    public static TheoryData<string, string?> InvalidTokens => new()
    {
        { "missing", null },
        { "malformed", "not-a-jwt" },
        { "untrusted signing key", TestTokens.Create(signWithUntrustedKey: true) },
        { "wrong issuer", TestTokens.Create(issuer: "https://evil.test/realms/transaction-platform") },
        { "wrong audience", TestTokens.Create(audience: "account") },
        { "expired beyond clock skew", TestTokens.Create(expires: DateTime.UtcNow.AddMinutes(-2)) },
        { "symmetric algorithm", TestTokens.Create(algorithm: SecurityAlgorithms.HmacSha256) },
    };

    private async Task<HttpResponseMessage> SendAsync(HttpMethod method, string path, string? token, object? body = null)
    {
        using var request = new HttpRequestMessage(method, path) { Content = body is null ? null : JsonContent.Create(body) };
        if (token is not null)
        {
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        }

        return await host.CreateClient().SendAsync(request);
    }

    [Theory]
    [MemberData(nameof(InvalidTokens))]
    public async Task Invalid_or_missing_token_is_rejected_with_401(string caseName, string? token)
    {
        using var response = await SendAsync(HttpMethod.Get, $"/orders/{Guid.NewGuid()}", token);

        Assert.True(response.StatusCode == HttpStatusCode.Unauthorized, $"{caseName}: {response.StatusCode}");
        Assert.Equal("Bearer", response.Headers.WwwAuthenticate.Single().Scheme);
    }

    [Fact]
    public async Task Valid_token_without_permissions_is_forbidden()
    {
        using var response = await SendAsync(HttpMethod.Get, $"/orders/{Guid.NewGuid()}", TestTokens.Create(permissions: []));

        Assert.Equal(HttpStatusCode.Forbidden, response.StatusCode);
    }

    [Fact]
    public async Task Read_permission_allows_reading_but_not_creating_orders()
    {
        var readerToken = TestTokens.Create(permissions: ["orders.read"]);

        using var read = await SendAsync(HttpMethod.Get, $"/orders/{Guid.NewGuid()}", readerToken);
        using var create = await SendAsync(HttpMethod.Post, "/orders", readerToken, TestOrders.Normal());

        Assert.Equal(HttpStatusCode.NotFound, read.StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, create.StatusCode);
    }

    [Fact]
    public async Task Health_endpoints_need_no_token()
    {
        using var response = await host.CreateClient().GetAsync("/health/live");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
    }

    public void Dispose() => host.Dispose();
}
