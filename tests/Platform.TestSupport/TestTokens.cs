using System.Security.Cryptography;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;

namespace Platform.TestSupport;

// Issues tokens the way the local Keycloak realm does, signed with a key only the tests know.
// Hosts configured with UseTestAuthentication trust exactly this key and issuer.
public static class TestTokens
{
    public const string Issuer = "https://identity.test/realms/transaction-platform";
    public const string Audience = "order-api";

    private static readonly RsaSecurityKey SigningKey = new(RSA.Create(2048)) { KeyId = "test-signing-key" };
    private static readonly RsaSecurityKey UntrustedKey = new(RSA.Create(2048)) { KeyId = "test-signing-key" };

    public static readonly string[] AllPermissions = ["orders.read", "orders.write", "traces.debug"];

    public static string Create(
        IEnumerable<string>? permissions = null,
        string issuer = Issuer,
        string audience = Audience,
        DateTime? expires = null,
        bool signWithUntrustedKey = false,
        string? algorithm = null)
    {
        var now = DateTime.UtcNow;
        var claims = new Dictionary<string, object>
        {
            ["sub"] = "service-account-test",
            ["azp"] = "scenario-runner",
            ["permissions"] = (permissions ?? AllPermissions).ToArray(),
        };
        SigningCredentials credentials = algorithm == SecurityAlgorithms.HmacSha256
            ? new SigningCredentials(new SymmetricSecurityKey(RandomNumberGenerator.GetBytes(32)), SecurityAlgorithms.HmacSha256)
            : new SigningCredentials(signWithUntrustedKey ? UntrustedKey : SigningKey, SecurityAlgorithms.RsaSha256);

        var expiry = expires ?? now.AddMinutes(5);
        return new JsonWebTokenHandler().CreateToken(new SecurityTokenDescriptor
        {
            Issuer = issuer,
            Audience = audience,
            Claims = claims,
            IssuedAt = expiry.AddMinutes(-5),
            NotBefore = expiry.AddMinutes(-5),
            Expires = expiry,
            SigningCredentials = credentials,
        });
    }

    public static ServiceHost<TMarker> UseTestAuthentication<TMarker>(this ServiceHost<TMarker> host)
        where TMarker : class =>
        host.WithSetting("Auth:Issuer", Issuer)
            .WithSetting("Auth:Audience", Audience)
            .WithSetting("Auth:MetadataAddress", "http://identity.test/.well-known/openid-configuration")
            .WithSetting("Auth:RequireHttpsMetadata", "false")
            .WithServices(services => services.PostConfigure<JwtBearerOptions>(JwtBearerDefaults.AuthenticationScheme, options =>
            {
                // A static configuration replaces the metadata download, so no identity provider is needed.
                var configuration = new OpenIdConnectConfiguration { Issuer = Issuer };
                configuration.SigningKeys.Add(SigningKey);
                options.Configuration = configuration;
                // The handler prefers the configuration manager, which by now points at the metadata URL.
                options.ConfigurationManager = new StaticConfigurationManager<OpenIdConnectConfiguration>(configuration);
            }));
}
