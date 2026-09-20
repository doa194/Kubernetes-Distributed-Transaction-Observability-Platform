using System.Net;
using Microsoft.AspNetCore.HttpOverrides;

namespace OrderService.Infrastructure;

// OrderService runs behind Kong, which terminates TLS. Kong reports the original scheme and client
// address in X-Forwarded-* headers. Those headers are trusted only when the connection really comes
// from the configured proxy network; anyone else could send them to pretend to be HTTPS.
// The Host header is preserved by Kong, so the public host and port need no forwarding header.
public static class ForwardedHeadersSetup
{
    public const string TrustedNetworkKey = "ForwardedHeaders:TrustedNetwork";

    public static IServiceCollection AddTrustedForwardedHeaders(this IServiceCollection services, IConfiguration configuration)
    {
        var trustedNetwork = configuration[TrustedNetworkKey];
        services.Configure<ForwardedHeadersOptions>(options =>
        {
            options.ForwardedHeaders = ForwardedHeaders.XForwardedFor | ForwardedHeaders.XForwardedProto;
            // Only the proxy directly in front of OrderService counts.
            options.ForwardLimit = 1;
            options.KnownProxies.Clear();
            options.KnownIPNetworks.Clear();
            if (!string.IsNullOrWhiteSpace(trustedNetwork))
            {
                options.KnownIPNetworks.Add(System.Net.IPNetwork.Parse(trustedNetwork));
            }
            else
            {
                options.KnownProxies.Add(IPAddress.Loopback);
            }
        });
        return services;
    }
}
