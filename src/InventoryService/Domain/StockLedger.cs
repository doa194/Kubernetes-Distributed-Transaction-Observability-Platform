// Stock levels and reservations of InventoryService.
namespace InventoryService.Domain;

public sealed record ReservationLine(string Sku, int Quantity);

public enum ReservationStatus
{
    Reserved,
    Released,
}

public sealed record Reservation(Guid OrderId, IReadOnlyList<ReservationLine> Lines, ReservationStatus Status);

public enum ReserveOutcome
{
    Reserved,
    AlreadyReserved,
    InsufficientStock,
    UnknownSku,
}

public sealed record ReserveResult(ReserveOutcome Outcome, Reservation? Reservation, string? Sku = null);

public sealed class StockItem
{
    public string Sku { get; set; } = string.Empty;

    public int Available { get; set; }
}

public sealed class StockOptions
{
    public const string SectionName = "Stock";

    public List<StockItem> Items { get; set; } = [];
}

// Stock levels and reservations, keyed by order id.
// Reserving is idempotent per order: a retried request returns the existing reservation instead
// of reserving twice. Releasing is idempotent too, so undo calls can be repeated safely.
// One lock keeps multi-line reservations all-or-nothing; this single-replica service does not
// need anything more elaborate.
public sealed class StockLedger
{
    public const int ReservationCapacity = 50_000;

    private static readonly StockItem[] DefaultStock =
    [
        new() { Sku = "SKU-1001", Available = 1_000_000 },
        new() { Sku = "SKU-1002", Available = 1_000_000 },
        new() { Sku = "SKU-2001", Available = 1_000_000 },
        // Never in stock, so a business rejection can be produced with plain data.
        new() { Sku = "SKU-9000", Available = 0 },
    ];

    private readonly Lock gate = new();
    private readonly Dictionary<string, int> available;
    private readonly Dictionary<Guid, Reservation> reservations = [];
    private readonly Queue<Guid> reservationOrder = new();

    public StockLedger(StockOptions options)
    {
        var items = options.Items.Count > 0 ? options.Items : [.. DefaultStock];
        available = items.ToDictionary(item => item.Sku, item => item.Available, StringComparer.Ordinal);
    }

    public ReserveResult Reserve(Guid orderId, IReadOnlyList<ReservationLine> lines)
    {
        lock (gate)
        {
            if (reservations.TryGetValue(orderId, out var existing) && existing.Status == ReservationStatus.Reserved)
            {
                return new ReserveResult(ReserveOutcome.AlreadyReserved, existing);
            }

            foreach (var line in lines)
            {
                if (!available.TryGetValue(line.Sku, out var stock))
                {
                    return new ReserveResult(ReserveOutcome.UnknownSku, null, line.Sku);
                }

                if (stock < line.Quantity)
                {
                    return new ReserveResult(ReserveOutcome.InsufficientStock, null, line.Sku);
                }
            }

            foreach (var line in lines)
            {
                available[line.Sku] -= line.Quantity;
            }

            var reservation = new Reservation(orderId, lines, ReservationStatus.Reserved);
            reservations[orderId] = reservation;
            reservationOrder.Enqueue(orderId);
            TrimOldReservations();
            return new ReserveResult(ReserveOutcome.Reserved, reservation);
        }
    }

    // Returns null when no reservation exists for the order.
    public Reservation? Release(Guid orderId)
    {
        lock (gate)
        {
            if (!reservations.TryGetValue(orderId, out var reservation))
            {
                return null;
            }

            if (reservation.Status == ReservationStatus.Released)
            {
                return reservation;
            }

            foreach (var line in reservation.Lines)
            {
                available[line.Sku] += line.Quantity;
            }

            var released = reservation with { Status = ReservationStatus.Released };
            reservations[orderId] = released;
            return released;
        }
    }

    public int AvailableFor(string sku)
    {
        lock (gate)
        {
            return available.GetValueOrDefault(sku);
        }
    }

    private void TrimOldReservations()
    {
        while (reservations.Count > ReservationCapacity && reservationOrder.TryDequeue(out var oldest))
        {
            reservations.Remove(oldest);
        }
    }
}
