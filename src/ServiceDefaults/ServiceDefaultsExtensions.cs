using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Diagnostics.HealthChecks;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using ServiceDefaults.Faults;
using ServiceDefaults.Health;
using ServiceDefaults.Hosting;
using ServiceDefaults.Telemetry;

namespace ServiceDefaults;

// Describes a service to the shared plumbing: its telemetry name, the operations that fault rules
// may target and the ActivitySources of its own business spans.
public sealed record PlatformServiceInfo(string Name, IReadOnlyCollection<string> FaultOperations, IReadOnlyCollection<string>? ActivitySources = null)
{
    public IReadOnlyCollection<string> ActivitySources { get; init; } = ActivitySources ?? [];
}

// One call per service wires the behavior every service must share: separated ports, JSON logs
// with trace ids, problem details, graceful shutdown, health checks and fault injection.
public static class ServiceDefaultsExtensions
{
    public const string ReadyTag = "ready";

    public static WebApplicationBuilder AddServiceDefaults(this WebApplicationBuilder builder, PlatformServiceInfo service)
    {
        builder.Services.AddSingleton(service);
        builder.Services.TryAddSingleton(TimeProvider.System);

        // Optional seed data (catalog prices, stock levels) mounted from a ConfigMap in Kubernetes.
        var seedPath = builder.Configuration["SEED_CONFIG_PATH"] ?? "/etc/txplatform/seed.json";
        builder.Configuration.AddJsonFile(seedPath, optional: true, reloadOnChange: false);

        var portsSection = builder.Configuration.GetSection(PortOptions.SectionName);
        builder.Services.Configure<PortOptions>(portsSection);
        var ports = portsSection.Get<PortOptions>() ?? new PortOptions();
        if (ports.SeparationEnabled)
        {
            builder.WebHost.ConfigureKestrel(kestrel =>
            {
                kestrel.ListenAnyIP(ports.Business);
                kestrel.ListenAnyIP(ports.Management);
            });
        }

        // Structured JSON logs carry trace and span ids, so a log line leads straight to its trace.
        builder.Logging.ClearProviders();
        builder.Logging.AddJsonConsole(options =>
        {
            options.IncludeScopes = true;
            options.UseUtcTimestamp = true;
            options.TimestampFormat = "yyyy-MM-ddTHH:mm:ss.fffZ ";
        });
        builder.Logging.Configure(options => options.ActivityTrackingOptions = ActivityTrackingOptions.TraceId | ActivityTrackingOptions.SpanId);
        builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
        builder.Logging.AddFilter("System.Net.Http.HttpClient", LogLevel.Warning);
        // Every retry is already a span event; logging each attempt as a warning would only add noise.
        builder.Logging.AddFilter("Polly", LogLevel.Error);

        builder.Services.ConfigureHttpJsonOptions(options => options.SerializerOptions.Converters.Add(new JsonStringEnumConverter()));
        builder.Services.AddProblemDetails();

        // Shorter than the pod's termination grace period, so in-flight requests finish before Kubernetes kills the process.
        builder.Services.Configure<HostOptions>(options => options.ShutdownTimeout = TimeSpan.FromSeconds(20));

        builder.Services.AddHealthChecks()
            .AddCheck<ShutdownReadinessCheck>("shutdown", tags: [ReadyTag])
            .AddCheck<FaultReadinessCheck>("fault-readiness", tags: [ReadyTag]);

        builder.Services.Configure<FaultOptions>(builder.Configuration.GetSection(FaultOptions.SectionName));
        builder.Services.AddSingleton<FaultRuleStore>();
        builder.Services.AddSingleton<IFaultStrategy, LatencyStrategy>();
        builder.Services.AddSingleton<IFaultStrategy, HttpErrorStrategy>();
        builder.Services.AddSingleton<IFaultStrategy, TimeoutStrategy>();
        builder.Services.AddSingleton<IFaultStrategy, IntermittentStrategy>();
        builder.Services.AddSingleton<IFaultStrategy, BusinessRejectionStrategy>();
        builder.Services.AddSingleton<FaultInjector>();

        if (!string.Equals(builder.Configuration["Telemetry:Enabled"], "false", StringComparison.OrdinalIgnoreCase))
        {
            builder.AddPlatformTelemetry(service);
        }

        return builder;
    }

    public static WebApplication UseServiceDefaults(this WebApplication app)
    {
        var ports = app.Services.GetRequiredService<IOptions<PortOptions>>().Value;
        app.Use(async (context, next) =>
        {
            if (!PortSeparation.IsAllowed(context.Request.Path, context.Connection.LocalPort, ports))
            {
                context.Response.StatusCode = StatusCodes.Status404NotFound;
                return;
            }

            await next(context);
        });
        app.UseExceptionHandler();
        // Empty error responses (for example 401 or 404) get a problem-details body too.
        app.UseStatusCodePages();
        return app;
    }

    public static WebApplication MapManagementEndpoints(this WebApplication app)
    {
        // Liveness runs no checks: the process answering at all means it can keep running.
        app.MapHealthChecks("/health/live", new HealthCheckOptions { Predicate = _ => false });
        app.MapHealthChecks("/health/ready", new HealthCheckOptions { Predicate = check => check.Tags.Contains(ReadyTag) });

        if (app.Services.GetRequiredService<IOptions<FaultOptions>>().Value.Enabled)
        {
            var service = app.Services.GetRequiredService<PlatformServiceInfo>();
            FaultEndpoints.Map(app, service.FaultOperations);
        }

        return app;
    }
}
