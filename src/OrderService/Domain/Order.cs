// The order itself: its lines, its state history and the failure and compensation it recorded.
namespace OrderService.Domain;

public sealed record OrderLine(string Sku, int Quantity, decimal UnitPrice)
{
    public decimal LineTotal => UnitPrice * Quantity;
}

public sealed record StateTransition(OrderState From, OrderState To, DateTimeOffset At);

public sealed record OrderFailure(FailureStage Stage, FailureKind Kind, string Reason);

public enum CompensationAction
{
    VoidPayment,
    ReleaseInventory,
}

public enum CompensationStatus
{
    Succeeded,
    Failed,
}

public sealed record CompensationStepResult(CompensationAction Action, CompensationStatus Status, string Detail);

// Immutable copy of an order, safe to read while the coordinator keeps working on the order.
public sealed record OrderSnapshot(
    Guid Id,
    OrderState State,
    IReadOnlyList<OrderLine> Lines,
    decimal TotalAmount,
    string Currency,
    string ShippingZone,
    OrderFailure? Failure,
    IReadOnlyList<CompensationStepResult> Compensation,
    IReadOnlyList<StateTransition> History,
    DateTimeOffset CreatedAt,
    DateTimeOffset UpdatedAt);

// The order aggregate. Only the coordinator changes it, and only through the state machine.
public sealed class Order(Guid id, IReadOnlyList<OrderLine> lines, string currency, string paymentToken, string shippingZone, DateTimeOffset createdAt)
{
    private readonly List<StateTransition> history = [];
    private readonly List<CompensationStepResult> compensation = [];

    public Guid Id { get; } = id;

    public IReadOnlyList<OrderLine> Lines { get; } = lines;

    public string Currency { get; } = currency;

    // An opaque payment reference, never card data. It is passed to PaymentService but never
    // returned by the API, logged or recorded in telemetry.
    public string PaymentToken { get; } = paymentToken;

    public string ShippingZone { get; } = shippingZone;

    public OrderState State { get; private set; } = OrderState.Pending;

    public OrderFailure? Failure { get; private set; }

    public DateTimeOffset CreatedAt { get; } = createdAt;

    public DateTimeOffset UpdatedAt { get; private set; } = createdAt;

    public decimal TotalAmount => Lines.Sum(line => line.LineTotal);

    public int ItemCount => Lines.Sum(line => line.Quantity);

    public StateTransition TransitionTo(OrderState target, DateTimeOffset at)
    {
        if (!OrderStateMachine.CanTransition(State, target))
        {
            throw new InvalidOperationException($"Order {Id} cannot move from {State} to {target}.");
        }

        var transition = new StateTransition(State, target, at);
        State = target;
        UpdatedAt = at;
        history.Add(transition);
        return transition;
    }

    public StateTransition Fail(OrderFailure failure, DateTimeOffset at)
    {
        var transition = TransitionTo(OrderStateMachine.FailureStateFor(failure.Stage), at);
        Failure = failure;
        return transition;
    }

    public void RecordCompensation(CompensationStepResult result) => compensation.Add(result);

    public OrderSnapshot ToSnapshot() =>
        new(Id, State, Lines, TotalAmount, Currency, ShippingZone, Failure, [.. compensation], [.. history], CreatedAt, UpdatedAt);
}
