// FraudService: stateless risk evaluation of orders. Private to the cluster; runs two replicas.
using FraudService.Features;
using ServiceDefaults;
using ServiceDefaults.Telemetry;

var builder = WebApplication.CreateBuilder(args);
builder.AddServiceDefaults(new PlatformServiceInfo("fraud-service", [EvaluationEndpoints.EvaluateOperation]));
builder.Services.AddFraudFeatures(builder.Configuration);

var app = builder.Build();
app.UseServiceDefaults();
app.UseBaggageTags();
app.MapEvaluationEndpoints();
app.MapManagementEndpoints();
app.Run();
