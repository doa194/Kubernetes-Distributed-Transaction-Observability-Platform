using System.Diagnostics;
using System.Text.RegularExpressions;
using OpenTelemetry;
using OrderService.Infrastructure.Auth;
using ServiceDefaults.Telemetry;

namespace OrderService.Infrastructure.Telemetry;

// OrderService is where outside requests enter the transaction, so it decides which identifiers
// are trusted. Incoming baggage is discarded, because an outside caller could otherwise inject
// arbitrary attributes into every span. Only validated ids are placed into baggage for downstream
// services. The response tells the caller its trace id (W3C traceresponse) and correlation id.
public static partial class CorrelationBoundary
{
    public const string CorrelationHeader = "X-Correlation-ID";
    public const string ScenarioHeader = "X-Scenario-Id";
    public const string ScenarioRunHeader = "X-Scenario-Run-Id";
    public const string KongRequestHeader = "X-Kong-Request-Id";
    public const string DebugTraceHeader = "X-Debug-Trace";

    // Plain, short identifiers only: letters, digits and . _ : - (a UUID fits).
    public static bool IsValidId(string? value) => value is not null && IdPattern().IsMatch(value);

    public static string CorrelationIdOrNew(string? supplied) => IsValidId(supplied) ? supplied! : Guid.NewGuid().ToString();

    public static WebApplication UseCorrelationBoundary(this WebApplication app)
    {
        app.Use(async (context, next) =>
        {
            var headers = context.Request.Headers;
            var correlationId = CorrelationIdOrNew(headers[CorrelationHeader].ToString());

            Baggage.ClearBaggage();
            Baggage.SetBaggage(TelemetryConventions.CorrelationId, correlationId);
            SetIfValid(TelemetryConventions.ScenarioId, headers[ScenarioHeader].ToString());
            SetIfValid(TelemetryConventions.ScenarioRunId, headers[ScenarioRunHeader].ToString());

            var server = Activity.Current;
            BaggageTagProcessor.CopyAllowedBaggage(server);
            if (IsValidId(headers[KongRequestHeader].ToString()))
            {
                server?.SetTag(TelemetryConventions.KongRequestId, headers[KongRequestHeader].ToString());
            }

            context.Response.OnStarting(() =>
            {
                context.Response.Headers[CorrelationHeader] = correlationId;
                if (server is not null)
                {
                    context.Response.Headers["traceresponse"] = $"00-{server.TraceId}-{server.SpanId}-{(server.Recorded ? "01" : "00")}";
                }

                return Task.CompletedTask;
            });

            await next(context);
        });
        return app;
    }

    // Runs after authentication: only callers with the traces.debug permission can ask for their
    // trace to be kept regardless of sampling.
    public static WebApplication UseDebugTraceMarker(this WebApplication app)
    {
        app.Use(async (context, next) =>
        {
            if (Permissions.MayRequestDebugTrace(context.User, context.Request.Headers[DebugTraceHeader].ToString()))
            {
                Activity.Current?.SetTag(TelemetryConventions.SamplingDebug, true);
            }

            await next(context);
        });
        return app;
    }

    private static void SetIfValid(string key, string? value)
    {
        if (IsValidId(value))
        {
            Baggage.SetBaggage(key, value);
        }
    }

    [GeneratedRegex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    private static partial Regex IdPattern();
}
