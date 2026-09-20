using System.Diagnostics;
using System.Net.Http.Json;
using System.Text.Json;
using OpenTelemetry;
using OpenTelemetry.Trace;
using OrderService.Infrastructure.Telemetry;
using Platform.TestSupport;
using ServiceDefaults.Telemetry;

namespace Platform.IntegrationTests;

// Runs all five services on real Kestrel ports in the test process, wired together the way
// Kubernetes wires them, so timeouts, retries and connection failures behave like real HTTP.
// One tracer provider built with the production tracing rules records every span in memory.
public sealed class PlatformFixture : IAsyncLifetime
{
    private readonly List<IDisposable> hosts = [];
    private readonly Dictionary<int, string> servicesByPort = [];
    private TracerProvider? tracerProvider;

    public HttpClient Orders { get; private set; } = null!;

    public HttpClient Inventory { get; private set; } = null!;

    public HttpClient Fraud { get; private set; } = null!;

    public HttpClient Payment { get; private set; } = null!;

    public HttpClient Shipping { get; private set; } = null!;

    public ServiceHost<ShippingService.AssemblyMarker> ShippingHost { get; private set; } = null!;

    public SpanCollection Spans { get; } = new();

    public ValueTask InitializeAsync()
    {
        tracerProvider = PlatformTelemetry
            .ConfigureTracing(Sdk.CreateTracerProviderBuilder(), [OrderTelemetry.SourceName])
            .AddInMemoryExporter(Spans)
            .Build();

        var inventory = Start("inventory-service", new ServiceHost<InventoryService.AssemblyMarker>());
        var fraud = Start("fraud-service", new ServiceHost<FraudService.AssemblyMarker>());
        var payment = Start("payment-service", new ServiceHost<PaymentService.AssemblyMarker>());
        ShippingHost = new ServiceHost<ShippingService.AssemblyMarker>();
        var shipping = Start("shipping-service", ShippingHost);

        var orders = Start("order-service", ConfigureOrderService(new ServiceHost<OrderService.AssemblyMarker>(), inventory, fraud, payment, shipping));

        Orders = Client(orders);
        Orders.DefaultRequestHeaders.Authorization = new("Bearer", TestTokens.Create());
        Inventory = Client(inventory);
        Fraud = Client(fraud);
        Payment = Client(payment);
        Shipping = Client(shipping);
        return ValueTask.CompletedTask;
    }

    public static ServiceHost<OrderService.AssemblyMarker> ConfigureOrderService(ServiceHost<OrderService.AssemblyMarker> host, Uri inventory, Uri fraud, Uri payment, Uri shipping) =>
        host.UseTestAuthentication()
            .WithSetting("Dependencies:InventoryBaseUrl", inventory.ToString())
            .WithSetting("Dependencies:FraudBaseUrl", fraud.ToString())
            .WithSetting("Dependencies:PaymentBaseUrl", payment.ToString())
            .WithSetting("Dependencies:ShippingBaseUrl", shipping.ToString());

    // In-process hosts share one process, so the service of a server or client span is
    // identified by the port it was served on or sent to.
    public string? ServiceOf(Activity span) =>
        span.GetTagItem("server.port") is int port && servicesByPort.TryGetValue(port, out var service) ? service : null;

    // Clears every fault rule and closes circuit breakers so tests cannot influence each other.
    public async Task ResetAsync()
    {
        foreach (var client in new[] { Inventory, Fraud, Payment, Shipping })
        {
            await Faults.ClearAsync(client);
        }

        using var reset = await Orders.PostAsync("/internal/resilience/reset", null);
        reset.EnsureSuccessStatusCode();
    }

    public async Task<JsonElement> LedgerAsync(Guid orderId) =>
        await Payment.GetFromJsonAsync<JsonElement>($"/internal/payments/{orderId}");

    public ValueTask DisposeAsync()
    {
        foreach (var host in Enumerable.Reverse(hosts))
        {
            host.Dispose();
        }

        tracerProvider?.Dispose();
        return ValueTask.CompletedTask;
    }

    private Uri Start<TMarker>(string service, ServiceHost<TMarker> host)
        where TMarker : class
    {
        hosts.Add(host);
        var address = host.StartOnKestrel();
        servicesByPort[address.Port] = service;
        return address;
    }

    private static HttpClient Client(Uri baseAddress) => new() { BaseAddress = baseAddress, Timeout = TimeSpan.FromSeconds(30) };
}

// Thread-safe span list: spans end on many threads at once.
public sealed class SpanCollection : ICollection<Activity>
{
    private readonly Lock gate = new();
    private readonly List<Activity> spans = [];

    public int Count
    {
        get
        {
            lock (gate)
            {
                return spans.Count;
            }
        }
    }

    public bool IsReadOnly => false;

    public void Add(Activity item)
    {
        lock (gate)
        {
            spans.Add(item);
        }
    }

    public IReadOnlyList<Activity> ForTrace(ActivityTraceId traceId)
    {
        lock (gate)
        {
            return [.. spans.Where(span => span.TraceId == traceId)];
        }
    }

    public IReadOnlyList<Activity> Snapshot()
    {
        lock (gate)
        {
            return [.. spans];
        }
    }

    public void Clear()
    {
        lock (gate)
        {
            spans.Clear();
        }
    }

    public bool Contains(Activity item) => Snapshot().Contains(item);

    public void CopyTo(Activity[] array, int arrayIndex) => Snapshot().ToList().CopyTo(array, arrayIndex);

    public bool Remove(Activity item)
    {
        lock (gate)
        {
            return spans.Remove(item);
        }
    }

    public IEnumerator<Activity> GetEnumerator() => Snapshot().GetEnumerator();

    System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator() => GetEnumerator();
}

[CollectionDefinition(Name)]
public sealed class PlatformCollection : ICollectionFixture<PlatformFixture>
{
    public const string Name = "platform";
}
