// Shipments of ShippingService and the zones it serves.
using System.Collections.Concurrent;

namespace ShippingService.Domain;

public sealed record Shipment(string ShipmentId, Guid OrderId, string Zone, int ItemCount);

public enum ShipmentOutcome
{
    Created,
    AlreadyCreated,
    ZoneNotServiceable,
}

// Shipments keyed by order id. Creating a shipment is idempotent, so a retried request returns
// the same shipment instead of shipping twice. The "restricted" zone is never serviceable,
// which gives the platform a deterministic business rejection.
public sealed class ShipmentBook
{
    public const int Capacity = 50_000;
    public static readonly IReadOnlySet<string> ServiceableZones = new HashSet<string>(StringComparer.Ordinal) { "domestic", "international" };

    private readonly ConcurrentDictionary<Guid, Shipment> shipments = new();
    private readonly ConcurrentQueue<Guid> insertionOrder = new();

    public (ShipmentOutcome Outcome, Shipment? Shipment) Create(Guid orderId, string zone, int itemCount)
    {
        if (!ServiceableZones.Contains(zone))
        {
            return (ShipmentOutcome.ZoneNotServiceable, null);
        }

        var candidate = new Shipment($"shp_{Guid.NewGuid():N}"[..20], orderId, zone, itemCount);
        var stored = shipments.GetOrAdd(orderId, candidate);
        if (!ReferenceEquals(stored, candidate))
        {
            return (ShipmentOutcome.AlreadyCreated, stored);
        }

        insertionOrder.Enqueue(orderId);
        while (shipments.Count > Capacity && insertionOrder.TryDequeue(out var oldest))
        {
            shipments.TryRemove(oldest, out _);
        }

        return (ShipmentOutcome.Created, stored);
    }
}
