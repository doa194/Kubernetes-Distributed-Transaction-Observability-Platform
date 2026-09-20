namespace OrderService.Domain;

// Result of one call to a dependency, already translated from HTTP into business terms.
public sealed record StageResult(bool Succeeded, FailureKind? Kind, string Reason)
{
    public static StageResult Success() => new(true, null, string.Empty);

    public static StageResult Failure(FailureKind kind, string reason) => new(false, kind, reason);
}

// Decides what the concurrent Inventory + Fraud stage means for the order.
public static class FanOutResolution
{
    // Inventory is checked first so that a double failure always produces the same state.
    public static OrderFailure? Resolve(StageResult inventory, StageResult fraud)
    {
        if (!inventory.Succeeded)
        {
            return new OrderFailure(FailureStage.Inventory, inventory.Kind!.Value, inventory.Reason);
        }

        return fraud.Succeeded ? null : new OrderFailure(FailureStage.Fraud, fraud.Kind!.Value, fraud.Reason);
    }
}

// Which earlier steps must be undone after a failure, in the order they must run.
// A technical failure (timeout, unavailable, error) is ambiguous: the dependency may have
// completed the work even though no reply arrived. Undo requests are idempotent, so they are
// sent anyway. A business rejection means the dependency definitely did nothing.
public static class CompensationPlan
{
    public static IReadOnlyList<CompensationAction> For(OrderFailure failure) => (failure.Stage, failure.Kind) switch
    {
        (FailureStage.Inventory, FailureKind.BusinessRejection) => [],
        (FailureStage.Inventory, _) => [CompensationAction.ReleaseInventory],
        (FailureStage.Fraud, _) => [CompensationAction.ReleaseInventory],
        (FailureStage.Payment, FailureKind.BusinessRejection) => [CompensationAction.ReleaseInventory],
        (FailureStage.Payment, _) => [CompensationAction.VoidPayment, CompensationAction.ReleaseInventory],
        (FailureStage.Shipping, _) => [CompensationAction.VoidPayment, CompensationAction.ReleaseInventory],
        _ => [],
    };
}
