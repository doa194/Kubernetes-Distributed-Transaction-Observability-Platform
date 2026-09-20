using System.Diagnostics;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using ServiceDefaults.Hosting;

namespace ServiceDefaults.Telemetry;

// The number of the current attempt, set on each outgoing request by the caller's retry pipeline.
public static class HttpAttempt
{
    public static readonly HttpRequestOptionsKey<int> OptionsKey = new("txplatform.attempt");
}

// OpenTelemetry tracing shared by every service.
// - Every span is recorded here; the Jaeger Collector decides later which traces to keep.
// - Health and management endpoints create no spans, so probes never reach trace storage.
// - Request/response bodies and headers are never captured; URL query values stay redacted.
// - Kubernetes metadata (pod, node, namespace, deployment) arrives through OTEL_RESOURCE_ATTRIBUTES,
//   which the Helm chart fills from the Kubernetes Downward API.
public static class PlatformTelemetry
{
    internal static WebApplicationBuilder AddPlatformTelemetry(this WebApplicationBuilder builder, PlatformServiceInfo service)
    {
        var activitySources = service.ActivitySources;
        builder.Services.AddOpenTelemetry()
            .ConfigureResource(resource => resource.AddService(
                serviceName: service.Name,
                serviceVersion: builder.Configuration["SERVICE_VERSION"] ?? "dev",
                autoGenerateServiceInstanceId: builder.Configuration["OTEL_RESOURCE_ATTRIBUTES"] is null))
            .WithTracing(tracing =>
            {
                ConfigureTracing(tracing, activitySources);
                // Without a collector address (for example during local tests) spans are simply not exported.
                if (!string.IsNullOrWhiteSpace(builder.Configuration["OTEL_EXPORTER_OTLP_ENDPOINT"]))
                {
                    tracing.AddOtlpExporter();
                }
            });
        return builder;
    }

    // Also used by integration tests, so they exercise exactly the production tracing rules.
    public static TracerProviderBuilder ConfigureTracing(TracerProviderBuilder tracing, IEnumerable<string> activitySources) =>
        tracing
            .SetSampler(new ParentBasedSampler(new AlwaysOnSampler()))
            .AddSource([.. activitySources])
            .AddAspNetCoreInstrumentation(options =>
            {
                options.Filter = context => !PortSeparation.IsManagementPath(context.Request.Path);
                options.RecordException = true;
                // The caller's IP address is personal data and is never recorded.
                options.EnrichWithHttpRequest = (activity, _) => activity.SetTag("client.address", null);
            })
            .AddHttpClientInstrumentation(options =>
            {
                options.RecordException = true;
                options.EnrichWithHttpRequestMessage = (activity, request) =>
                {
                    if (request.Options.TryGetValue(HttpAttempt.OptionsKey, out var attempt))
                    {
                        activity.SetTag(TelemetryConventions.RetryAttempt, attempt);
                    }
                };
            })
            .AddProcessor(new BaggageTagProcessor());

    // Copies allow-listed baggage (scenario and correlation ids) to spans started inside the
    // service, so every span of a request can be found by those ids.
    public static WebApplication UseBaggageTags(this WebApplication app)
    {
        app.Use(async (context, next) =>
        {
            // Server spans start before incoming baggage is read, so they are tagged here instead.
            BaggageTagProcessor.CopyAllowedBaggage(Activity.Current);
            await next(context);
        });
        return app;
    }
}

public sealed class BaggageTagProcessor : BaseProcessor<Activity>
{
    public const int MaxValueLength = 128;

    public override void OnStart(Activity data) => CopyAllowedBaggage(data);

    public static void CopyAllowedBaggage(Activity? activity)
    {
        if (activity is null)
        {
            return;
        }

        foreach (var (key, value) in Baggage.Current)
        {
            if (TelemetryConventions.AllowedBaggageKeys.Contains(key) && value is { Length: > 0 and <= MaxValueLength })
            {
                activity.SetTag(key, value);
            }
        }
    }
}
