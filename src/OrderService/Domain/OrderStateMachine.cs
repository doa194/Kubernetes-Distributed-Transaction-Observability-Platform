namespace OrderService.Domain;

// The only allowed state changes of an order.
// Every transition not listed here is a programming error and is rejected, which keeps the
// order history (and the state events in traces) trustworthy.
public static class OrderStateMachine
{
    private static readonly Dictionary<OrderState, OrderState[]> AllowedTransitions = new()
    {
        [OrderState.Pending] = [OrderState.InventoryReserved, OrderState.InventoryFailed, OrderState.FraudFailed],
        [OrderState.InventoryReserved] = [OrderState.PaymentAuthorized, OrderState.PaymentFailed],
        [OrderState.PaymentAuthorized] = [OrderState.ShipmentCreated, OrderState.ShippingFailed],
        [OrderState.ShipmentCreated] = [OrderState.Completed],
    };

    public static bool CanTransition(OrderState from, OrderState to) =>
        AllowedTransitions.TryGetValue(from, out var targets) && targets.Contains(to);

    public static bool IsFinal(OrderState state) => !AllowedTransitions.ContainsKey(state);

    public static OrderState FailureStateFor(FailureStage stage) => stage switch
    {
        FailureStage.Inventory => OrderState.InventoryFailed,
        FailureStage.Fraud => OrderState.FraudFailed,
        FailureStage.Payment => OrderState.PaymentFailed,
        FailureStage.Shipping => OrderState.ShippingFailed,
        _ => throw new ArgumentOutOfRangeException(nameof(stage), stage, null),
    };
}
