// Payment decisions and the rules that make retrying a payment safe.
using System.Security.Cryptography;
using System.Text;

namespace PaymentService.Domain;

public sealed record PaymentDecision(bool Approved, string Reason);

// Simulated payment provider rules. Test tokens produce declines deterministically, the same way
// real payment providers offer test cards for specific outcomes.
public static class PaymentRules
{
    public static PaymentDecision Decide(string paymentToken) => paymentToken switch
    {
        "tok_test_declined" => new PaymentDecision(false, "card_declined"),
        "tok_test_insufficient_funds" => new PaymentDecision(false, "insufficient_funds"),
        _ => new PaymentDecision(true, "approved"),
    };
}

public enum IdempotencyStatus
{
    InProgress,
    Completed,
}

public sealed record StoredResponse(int StatusCode, string Body);

public sealed record IdempotencyRecord(string Key, string Fingerprint, IdempotencyStatus Status, StoredResponse? Response);

public enum IdempotencyDecision
{
    StartNew,
    ReplayStored,
    RejectInProgress,
    RejectFingerprintMismatch,
}

// Decides how to treat a request that carries an Idempotency-Key.
// - No record: process it.
// - Same key, same request, finished: return the stored answer (a retry after a lost response).
// - Same key, still running: tell the caller to retry later instead of authorizing twice.
// - Same key, different request: refuse, because the key is being misused.
public static class IdempotencyRules
{
    public static IdempotencyDecision Decide(IdempotencyRecord? existing, string fingerprint)
    {
        if (existing is null)
        {
            return IdempotencyDecision.StartNew;
        }

        if (existing.Fingerprint != fingerprint)
        {
            return IdempotencyDecision.RejectFingerprintMismatch;
        }

        return existing.Status == IdempotencyStatus.Completed
            ? IdempotencyDecision.ReplayStored
            : IdempotencyDecision.RejectInProgress;
    }

    public static string Fingerprint(Guid orderId, decimal amount, string currency, string paymentToken)
    {
        var canonical = FormattableString.Invariant($"{orderId:N}|{amount:0.00}|{currency}|{paymentToken}");
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)));
    }
}
