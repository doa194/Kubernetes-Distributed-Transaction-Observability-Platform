using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Logging;

namespace Platform.TestSupport;

// Starts one service in the test process.
// Port separation is switched off (ports 0) because test hosts listen on a single address, and
// fault injection is switched on so tests can simulate dependency failures.
public sealed class ServiceHost<TMarker> : WebApplicationFactory<TMarker>
    where TMarker : class
{
    private readonly Dictionary<string, string?> settings = new(StringComparer.Ordinal)
    {
        ["Ports:Business"] = "0",
        ["Ports:Management"] = "0",
        ["Faults:Enabled"] = "true",
        // Tracing listeners are process-wide, so tests that inspect spans register one shared tracer
        // provider instead of one per in-process service.
        ["Telemetry:Enabled"] = "false",
    };

    private readonly List<Action<IServiceCollection>> serviceOverrides = [];

    public ServiceHost<TMarker> WithSetting(string key, string? value)
    {
        settings[key] = value;
        return this;
    }

    public ServiceHost<TMarker> WithServices(Action<IServiceCollection> configure)
    {
        serviceOverrides.Add(configure);
        return this;
    }

    // Runs the service on real Kestrel with a random port, for tests that need real network behavior
    // such as timeouts, retries or connection failures.
    public Uri StartOnKestrel()
    {
        UseKestrel(0);
        StartServer();
        var address = Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.First();
        return new Uri(address.Replace("[::]", "127.0.0.1", StringComparison.Ordinal).Replace("0.0.0.0", "127.0.0.1", StringComparison.Ordinal));
    }

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        foreach (var (key, value) in settings)
        {
            builder.UseSetting(key, value);
        }

        builder.ConfigureServices(services =>
        {
            // Service logs would bury test output; tests assert on responses and telemetry instead.
            services.RemoveAll<ILoggerProvider>();
            foreach (var configure in serviceOverrides)
            {
                configure(services);
            }
        });
    }
}
