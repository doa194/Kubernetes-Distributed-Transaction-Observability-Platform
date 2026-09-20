// InventoryService: reserves and releases stock for orders. Private to the cluster.
using InventoryService.Features;
using ServiceDefaults;
using ServiceDefaults.Telemetry;

var builder = WebApplication.CreateBuilder(args);
builder.AddServiceDefaults(new PlatformServiceInfo("inventory-service", [ReservationEndpoints.ReserveOperation, ReservationEndpoints.ReleaseOperation]));
builder.Services.AddInventoryFeatures(builder.Configuration);

var app = builder.Build();
app.UseServiceDefaults();
app.UseBaggageTags();
app.MapReservationEndpoints();
app.MapManagementEndpoints();
app.Run();
