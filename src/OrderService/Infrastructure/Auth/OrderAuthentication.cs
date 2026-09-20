// Token validation and permission policies for the Order API.
using System.Security.Claims;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using Microsoft.Extensions.Options;
using Microsoft.IdentityModel.Tokens;
using ServiceDefaults;

namespace OrderService.Infrastructure.Auth;

public sealed class AuthOptions
{
    public const string SectionName = "Auth";

    // The public issuer URL that appears in tokens (how clients reach Keycloak).
    public string Issuer { get; set; } = string.Empty;

    public string Audience { get; set; } = "order-api";

    // Where OrderService fetches signing keys. Inside the cluster this is Keycloak's internal URL,
    // which differs from the public issuer URL.
    public string MetadataAddress { get; set; } = string.Empty;

    public bool RequireHttpsMetadata { get; set; } = true;

    public int ClockSkewSeconds { get; set; } = 30;
}

// Permission names issued by Keycloak in the "permissions" claim.
public static class Permissions
{
    public const string ClaimType = "permissions";
    public const string OrdersRead = "orders.read";
    public const string OrdersWrite = "orders.write";
    public const string TracesDebug = "traces.debug";

    // Asking for a fully retained trace is privileged: otherwise any caller could force the
    // platform to store every one of its traces.
    public static bool MayRequestDebugTrace(ClaimsPrincipal user, string? debugHeader) =>
        string.Equals(debugHeader, "true", StringComparison.OrdinalIgnoreCase)
        && user.HasClaim(Permissions.ClaimType, TracesDebug);
}

// JWT bearer authentication for the public Order API.
// Every token must be signed with RS256 by the configured Keycloak realm, be issued for the
// order-api audience and be unexpired; the permissions claim then decides what the caller may do.
public static class OrderAuthentication
{
    public const string SigningKeysCheck = "signing-keys";

    public static IServiceCollection AddOrderAuthentication(this IServiceCollection services, IConfiguration configuration)
    {
        services.Configure<AuthOptions>(configuration.GetSection(AuthOptions.SectionName));

        services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
            .AddJwtBearer();
        services.AddOptions<JwtBearerOptions>(JwtBearerDefaults.AuthenticationScheme)
            .Configure<IOptions<AuthOptions>>((jwt, authOptions) =>
            {
                var auth = authOptions.Value;
                jwt.MetadataAddress = auth.MetadataAddress;
                jwt.RequireHttpsMetadata = auth.RequireHttpsMetadata;
                // Keep claim names exactly as issued ("permissions", "sub") instead of legacy XML names.
                jwt.MapInboundClaims = false;
                // A token signed with an unknown key id (e.g. after Keycloak restarted and rotated its keys) requests
                // a fresh key download for the following requests. The default 5-minute throttle would reject new
                // tokens for too long, so key refreshes are allowed every 30 seconds.
                jwt.RefreshOnIssuerKeyNotFound = true;
                jwt.RefreshInterval = TimeSpan.FromSeconds(30);
                jwt.TokenValidationParameters = new TokenValidationParameters
                {
                    ValidIssuer = auth.Issuer,
                    ValidAudience = auth.Audience,
                    ValidateIssuer = true,
                    ValidateAudience = true,
                    ValidateLifetime = true,
                    ValidateIssuerSigningKey = true,
                    RequireExpirationTime = true,
                    RequireSignedTokens = true,
                    ValidAlgorithms = [SecurityAlgorithms.RsaSha256],
                    ClockSkew = TimeSpan.FromSeconds(auth.ClockSkewSeconds),
                };
            });

        services.AddAuthorizationBuilder()
            .AddPolicy(Permissions.OrdersRead, policy => policy.RequireAuthenticatedUser().RequireClaim(Permissions.ClaimType, Permissions.OrdersRead))
            .AddPolicy(Permissions.OrdersWrite, policy => policy.RequireAuthenticatedUser().RequireClaim(Permissions.ClaimType, Permissions.OrdersWrite));

        services.AddHealthChecks().AddCheck<SigningKeysReadinessCheck>(SigningKeysCheck, tags: [ServiceDefaultsExtensions.ReadyTag]);
        return services;
    }
}

// OrderService cannot validate any request until it has downloaded the signing keys once, so it
// reports not-ready until then. After the first success it stays ready: cached keys keep working
// even while Keycloak restarts.
internal sealed class SigningKeysReadinessCheck(IOptionsMonitor<JwtBearerOptions> options) : IHealthCheck
{
    private volatile bool keysLoaded;

    public async Task<HealthCheckResult> CheckHealthAsync(HealthCheckContext context, CancellationToken cancellationToken = default)
    {
        if (keysLoaded)
        {
            return HealthCheckResult.Healthy();
        }

        var jwt = options.Get(JwtBearerDefaults.AuthenticationScheme);
        if (jwt.Configuration is { SigningKeys.Count: > 0 })
        {
            keysLoaded = true;
            return HealthCheckResult.Healthy();
        }

        if (jwt.ConfigurationManager is null)
        {
            return HealthCheckResult.Unhealthy("no signing key source is configured");
        }

        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(TimeSpan.FromSeconds(3));
            var configuration = await jwt.ConfigurationManager.GetConfigurationAsync(timeout.Token);
            keysLoaded = configuration.SigningKeys.Count > 0;
            return keysLoaded ? HealthCheckResult.Healthy() : HealthCheckResult.Unhealthy("the identity provider returned no signing keys");
        }
        catch (Exception error) when (error is not OperationCanceledException || !cancellationToken.IsCancellationRequested)
        {
            return HealthCheckResult.Unhealthy("signing keys could not be loaded from the identity provider", error);
        }
    }
}
