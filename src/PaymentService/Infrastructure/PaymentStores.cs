using System.Collections.Concurrent;
using PaymentService.Domain;

namespace PaymentService.Infrastructure;

// Idempotency records kept in memory for the lifetime of this pod.
// Starting a request is atomic (TryAdd), so two identical requests arriving at the same time can
// never both be processed. Limitation: records do not survive a pod restart, which is why
// PaymentService runs a single replica; production would use a durable shared store.
public sealed class IdempotencyStore
{
    public const int Capacity = 100_000;

    private readonly ConcurrentDictionary<string, IdempotencyRecord> records = new(StringComparer.Ordinal);
    private readonly ConcurrentQueue<string> insertionOrder = new();

    public (IdempotencyDecision Decision, IdempotencyRecord? Existing) TryBegin(string key, string fingerprint)
    {
        var started = new IdempotencyRecord(key, fingerprint, IdempotencyStatus.InProgress, null);
        if (records.TryAdd(key, started))
        {
            insertionOrder.Enqueue(key);
            Trim();
            return (IdempotencyDecision.StartNew, null);
        }

        var existing = records.GetValueOrDefault(key);
        var decision = IdempotencyRules.Decide(existing, fingerprint);
        // The record may have been abandoned between TryAdd and the lookup; the caller simply retries.
        return (decision == IdempotencyDecision.StartNew ? IdempotencyDecision.RejectInProgress : decision, existing);
    }

    public void Complete(string key, StoredResponse response) =>
        records.AddOrUpdate(key, _ => throw new InvalidOperationException($"No in-progress record for {key}"), (_, record) => record with { Status = IdempotencyStatus.Completed, Response = response });

    // Removes an in-progress record whose request failed before any effect, so a retry can start fresh.
    public void Abandon(string key) => records.TryRemove(key, out _);

    private void Trim()
    {
        while (records.Count > Capacity && insertionOrder.TryDequeue(out var oldest))
        {
            records.TryRemove(oldest, out _);
        }
    }
}

public sealed record Authorization(string AuthorizationId, Guid OrderId, decimal Amount, string Currency, bool Voided);

public sealed record LedgerSummary(Guid OrderId, int Authorizations, int Voided);

// Simulated provider ledger. Its authorization count per order is what proves that retries never
// charge twice.
public sealed class PaymentLedger
{
    private readonly Lock gate = new();
    private readonly Dictionary<Guid, List<Authorization>> authorizations = [];

    public Authorization Authorize(Guid orderId, decimal amount, string currency)
    {
        var authorization = new Authorization($"auth_{Guid.NewGuid():N}"[..20], orderId, amount, currency, false);
        lock (gate)
        {
            if (!authorizations.TryGetValue(orderId, out var entries))
            {
                authorizations[orderId] = entries = [];
            }

            entries.Add(authorization);
        }

        return authorization;
    }

    // Voids every open authorization of the order and reports whether any existed.
    public bool Void(Guid orderId)
    {
        lock (gate)
        {
            if (!authorizations.TryGetValue(orderId, out var entries) || entries.Count == 0)
            {
                return false;
            }

            for (var index = 0; index < entries.Count; index++)
            {
                entries[index] = entries[index] with { Voided = true };
            }

            return true;
        }
    }

    public LedgerSummary Summarize(Guid orderId)
    {
        lock (gate)
        {
            var entries = authorizations.GetValueOrDefault(orderId) ?? [];
            return new LedgerSummary(orderId, entries.Count, entries.Count(entry => entry.Voided));
        }
    }
}
