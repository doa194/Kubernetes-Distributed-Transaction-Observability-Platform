using OrderService.Domain;
using OrderService.Features;
using OrderService.Features.CreateOrder;

namespace Platform.UnitTests.Orders;

// The state machine, failure precedence, compensation plan and HTTP mapping define what an order
// transaction means; the trace contracts later rely on exactly these rules.
public sealed class OrderStateMachineTests
{
    private static readonly DateTimeOffset Now = new(2026, 9, 17, 10, 0, 0, TimeSpan.Zero);

    private static Order NewOrder() =>
        new(Guid.NewGuid(), [new OrderLine("SKU-1001", 1, 12.50m)], "EUR", "tok_test_approved", "domestic", Now);

    [Fact]
    public void Successful_path_reaches_completed_and_records_every_step()
    {
        var order = NewOrder();

        order.TransitionTo(OrderState.InventoryReserved, Now);
        order.TransitionTo(OrderState.PaymentAuthorized, Now);
        order.TransitionTo(OrderState.ShipmentCreated, Now);
        order.TransitionTo(OrderState.Completed, Now);

        var snapshot = order.ToSnapshot();
        Assert.Equal(OrderState.Completed, snapshot.State);
        Assert.Equal([OrderState.InventoryReserved, OrderState.PaymentAuthorized, OrderState.ShipmentCreated, OrderState.Completed], snapshot.History.Select(t => t.To));
    }

    [Theory]
    [InlineData(OrderState.Pending, OrderState.PaymentAuthorized)]
    [InlineData(OrderState.Pending, OrderState.Completed)]
    [InlineData(OrderState.InventoryReserved, OrderState.ShipmentCreated)]
    [InlineData(OrderState.PaymentAuthorized, OrderState.InventoryFailed)]
    [InlineData(OrderState.Completed, OrderState.PaymentFailed)]
    [InlineData(OrderState.PaymentFailed, OrderState.Completed)]
    public void Skipping_steps_or_leaving_a_final_state_is_rejected(OrderState from, OrderState to)
    {
        Assert.False(OrderStateMachine.CanTransition(from, to));
    }

    [Fact]
    public void Failed_order_cannot_change_state_again()
    {
        var order = NewOrder();
        order.TransitionTo(OrderState.InventoryReserved, Now);
        order.Fail(new OrderFailure(FailureStage.Payment, FailureKind.Timeout, "timeout"), Now);

        Assert.Throws<InvalidOperationException>(() => order.TransitionTo(OrderState.PaymentAuthorized, Now));
        Assert.True(OrderStateMachine.IsFinal(order.State));
    }

    [Theory]
    [InlineData(FailureStage.Inventory, OrderState.InventoryFailed)]
    [InlineData(FailureStage.Fraud, OrderState.FraudFailed)]
    [InlineData(FailureStage.Payment, OrderState.PaymentFailed)]
    [InlineData(FailureStage.Shipping, OrderState.ShippingFailed)]
    public void Each_stage_fails_into_its_own_state(FailureStage stage, OrderState expected)
    {
        Assert.Equal(expected, OrderStateMachine.FailureStateFor(stage));
    }
}

public sealed class FanOutResolutionTests
{
    private static readonly StageResult Ok = StageResult.Success();

    [Fact]
    public void Both_checks_passing_lets_the_order_continue()
    {
        Assert.Null(FanOutResolution.Resolve(Ok, Ok));
    }

    [Fact]
    public void Inventory_failure_wins_when_both_checks_fail()
    {
        var failure = FanOutResolution.Resolve(
            StageResult.Failure(FailureKind.Timeout, "timeout"),
            StageResult.Failure(FailureKind.BusinessRejection, "amount_threshold"));

        Assert.Equal(new OrderFailure(FailureStage.Inventory, FailureKind.Timeout, "timeout"), failure);
    }

    [Fact]
    public void Fraud_rejection_fails_the_order_after_a_successful_reservation()
    {
        var failure = FanOutResolution.Resolve(Ok, StageResult.Failure(FailureKind.BusinessRejection, "amount_threshold"));

        Assert.Equal(FailureStage.Fraud, failure!.Stage);
    }
}

public sealed class CompensationPlanTests
{
    public static TheoryData<FailureStage, FailureKind, CompensationAction[]> Plans => new()
    {
        { FailureStage.Inventory, FailureKind.BusinessRejection, [] },
        // A timed-out reservation may exist, so it is released anyway.
        { FailureStage.Inventory, FailureKind.Timeout, [CompensationAction.ReleaseInventory] },
        { FailureStage.Fraud, FailureKind.BusinessRejection, [CompensationAction.ReleaseInventory] },
        // A declined payment authorized nothing; only the stock is released.
        { FailureStage.Payment, FailureKind.BusinessRejection, [CompensationAction.ReleaseInventory] },
        // A timed-out payment may have been authorized, so it is voided before releasing stock.
        { FailureStage.Payment, FailureKind.Timeout, [CompensationAction.VoidPayment, CompensationAction.ReleaseInventory] },
        { FailureStage.Shipping, FailureKind.DependencyError, [CompensationAction.VoidPayment, CompensationAction.ReleaseInventory] },
    };

    [Theory]
    [MemberData(nameof(Plans))]
    public void Undo_steps_depend_on_stage_and_failure_kind(FailureStage stage, FailureKind kind, CompensationAction[] expected)
    {
        Assert.Equal(expected, CompensationPlan.For(new OrderFailure(stage, kind, "reason")));
    }
}

public sealed class OrderOutcomeMappingTests
{
    [Theory]
    [InlineData(FailureKind.BusinessRejection, 422)]
    [InlineData(FailureKind.DependencyError, 502)]
    [InlineData(FailureKind.Unavailable, 503)]
    [InlineData(FailureKind.Timeout, 504)]
    public void Failure_kind_maps_to_http_status(FailureKind kind, int status)
    {
        Assert.Equal(status, OrderOutcomeMapping.StatusCodeFor(kind));
    }
}

public sealed class CreateOrderValidatorTests
{
    private static readonly PriceCatalog Catalog = new(new CatalogOptions());

    private static IReadOnlyDictionary<string, string[]> Validate(CreateOrderRequest request) => CreateOrderValidator.Validate(request, Catalog);

    [Fact]
    public void Valid_order_has_no_errors()
    {
        Assert.Empty(Validate(new CreateOrderRequest([new CreateOrderLine("SKU-1001", 2)], "tok_test_approved", "domestic")));
    }

    [Fact]
    public void Missing_body_is_rejected()
    {
        Assert.Contains("body", CreateOrderValidator.Validate(null, Catalog).Keys);
    }

    [Theory]
    [InlineData("sku-1001", "items[0].sku")]
    [InlineData("SKU-7777", "items[0].sku")]
    public void Malformed_or_unknown_sku_is_rejected(string sku, string field)
    {
        Assert.Contains(field, Validate(new CreateOrderRequest([new CreateOrderLine(sku, 1)], "tok_test_approved", "domestic")).Keys);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(101)]
    public void Quantity_outside_limits_is_rejected(int quantity)
    {
        Assert.Contains("items[0].quantity", Validate(new CreateOrderRequest([new CreateOrderLine("SKU-1001", quantity)], "tok_test_approved", "domestic")).Keys);
    }

    [Fact]
    public void Duplicate_lines_and_empty_orders_are_rejected()
    {
        Assert.Contains("items[1].sku", Validate(new CreateOrderRequest([new CreateOrderLine("SKU-1001", 1), new CreateOrderLine("SKU-1001", 1)], "tok_test_approved", "domestic")).Keys);
        Assert.Contains("items", Validate(new CreateOrderRequest([], "tok_test_approved", "domestic")).Keys);
    }

    [Fact]
    public void Too_many_lines_are_rejected()
    {
        var lines = Enumerable.Range(0, CreateOrderValidator.MaxLines + 1).Select(_ => new CreateOrderLine("SKU-1001", 1)).ToList();

        Assert.Contains("items", Validate(new CreateOrderRequest(lines, "tok_test_approved", "domestic")).Keys);
    }

    [Theory]
    [InlineData("4111111111111111")]
    [InlineData("tok_4111111111111111")]
    [InlineData("Bearer abc")]
    public void Card_numbers_and_non_token_values_are_never_accepted_as_payment_tokens(string token)
    {
        Assert.Contains("paymentToken", Validate(new CreateOrderRequest([new CreateOrderLine("SKU-1001", 1)], token, "domestic")).Keys);
    }

    [Fact]
    public void Unknown_shipping_zone_is_rejected()
    {
        Assert.Contains("shippingZone", Validate(new CreateOrderRequest([new CreateOrderLine("SKU-1001", 1)], "tok_test_approved", "moon")).Keys);
    }
}
