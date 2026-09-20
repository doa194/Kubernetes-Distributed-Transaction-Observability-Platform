using System.Collections.Concurrent;
using OrderService.Domain;

namespace OrderService.Infrastructure;

// Keeps order snapshots in memory. Persistence is intentionally not the focus of this platform.
// The store is capped and forgets the oldest orders, so long load tests cannot exhaust pod memory.
public sealed class InMemoryOrderRepository
{
    public const int Capacity = 20_000;

    private readonly ConcurrentDictionary<Guid, OrderSnapshot> orders = new();
    private readonly ConcurrentQueue<Guid> insertionOrder = new();

    public void Save(OrderSnapshot snapshot)
    {
        if (orders.TryAdd(snapshot.Id, snapshot))
        {
            insertionOrder.Enqueue(snapshot.Id);
            while (orders.Count > Capacity && insertionOrder.TryDequeue(out var oldest))
            {
                orders.TryRemove(oldest, out _);
            }

            return;
        }

        orders[snapshot.Id] = snapshot;
    }

    public OrderSnapshot? Find(Guid id) => orders.TryGetValue(id, out var snapshot) ? snapshot : null;
}
