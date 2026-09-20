// PaymentService: idempotent payment authorization and voids. Private to the cluster.
using PaymentService.Features;
using ServiceDefaults;
using ServiceDefaults.Telemetry;

var builder = WebApplication.CreateBuilder(args);
builder.AddServiceDefaults(new PlatformServiceInfo("payment-service", [PaymentEndpoints.AuthorizeOperation, PaymentEndpoints.VoidOperation]));
builder.Services.AddPaymentFeatures();

var app = builder.Build();
app.UseServiceDefaults();
app.UseBaggageTags();
app.MapPaymentEndpoints();
app.MapManagementEndpoints();
app.Run();
