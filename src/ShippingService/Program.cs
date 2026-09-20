// ShippingService: creates shipments for orders. Private to the cluster.
using ServiceDefaults;
using ServiceDefaults.Telemetry;
using ShippingService.Features;

var builder = WebApplication.CreateBuilder(args);
builder.AddServiceDefaults(new PlatformServiceInfo("shipping-service", [ShipmentEndpoints.CreateOperation]));
builder.Services.AddShippingFeatures();

var app = builder.Build();
app.UseServiceDefaults();
app.UseBaggageTags();
app.MapShipmentEndpoints();
app.MapManagementEndpoints();
app.Run();
