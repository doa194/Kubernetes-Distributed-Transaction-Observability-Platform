// OrderService: the public Order API and coordinator of the distributed order transaction.
// It is the only service reachable through the Kong gateway.
using OrderService.Features;
using OrderService.Infrastructure;
using OrderService.Infrastructure.Auth;
using OrderService.Infrastructure.Telemetry;
using ServiceDefaults;

var builder = WebApplication.CreateBuilder(args);
builder.AddServiceDefaults(new PlatformServiceInfo("order-service", FaultOperations: [], ActivitySources: [OrderTelemetry.SourceName]));
builder.Services.AddTrustedForwardedHeaders(builder.Configuration);
builder.Services.AddOrderAuthentication(builder.Configuration);
builder.Services.AddOrderFeatures(builder.Configuration);

var app = builder.Build();
app.UseForwardedHeaders();
app.UseServiceDefaults();
app.UseCorrelationBoundary();
app.UseAuthentication();
app.UseAuthorization();
app.UseDebugTraceMarker();
app.MapOrderEndpoints();
app.MapManagementEndpoints();
app.MapResilienceManagement();
app.Run();
