using FraudService.Domain;
using InventoryService.Domain;
using ShippingService.Domain;

namespace Platform.UnitTests.Services;

// Domain rules of the downstream services that produce deterministic business outcomes.
public sealed class StockLedgerTests
{
    private static StockLedger Ledger() => new(new StockOptions
    {
        Items = [new() { Sku = "SKU-1001", Available = 5 }, new() { Sku = "SKU-1002", Available = 1 }],
    });

    [Fact]
    public void Reserving_twice_for_the_same_order_reserves_once()
    {
        var ledger = Ledger();
        var orderId = Guid.NewGuid();

        var first = ledger.Reserve(orderId, [new ReservationLine("SKU-1001", 3)]);
        var second = ledger.Reserve(orderId, [new ReservationLine("SKU-1001", 3)]);

        Assert.Equal(ReserveOutcome.Reserved, first.Outcome);
        Assert.Equal(ReserveOutcome.AlreadyReserved, second.Outcome);
        Assert.Equal(2, ledger.AvailableFor("SKU-1001"));
    }

    [Fact]
    public void Multi_line_reservation_is_all_or_nothing()
    {
        var ledger = Ledger();

        var result = ledger.Reserve(Guid.NewGuid(), [new ReservationLine("SKU-1001", 2), new ReservationLine("SKU-1002", 2)]);

        Assert.Equal(ReserveOutcome.InsufficientStock, result.Outcome);
        Assert.Equal("SKU-1002", result.Sku);
        Assert.Equal(5, ledger.AvailableFor("SKU-1001"));
    }

    [Fact]
    public void Release_restores_stock_once_and_unknown_orders_report_nothing_to_release()
    {
        var ledger = Ledger();
        var orderId = Guid.NewGuid();
        ledger.Reserve(orderId, [new ReservationLine("SKU-1001", 4)]);

        ledger.Release(orderId);
        ledger.Release(orderId);

        Assert.Equal(5, ledger.AvailableFor("SKU-1001"));
        Assert.Null(ledger.Release(Guid.NewGuid()));
    }
}

public sealed class RiskRulesTests
{
    private static readonly RiskOptions Options = new();

    [Theory]
    [InlineData(5000.00, 10, true, "within_limits")]
    [InlineData(5000.01, 10, false, "amount_threshold")]
    [InlineData(100.00, 61, false, "quantity_threshold")]
    public void Orders_above_amount_or_quantity_limits_are_rejected(decimal total, int items, bool approved, string rule)
    {
        var decision = RiskRules.Evaluate(total, items, Options);

        Assert.Equal(approved, decision.Approved);
        Assert.Equal(rule, decision.Rule);
    }
}

public sealed class ShipmentBookTests
{
    [Fact]
    public void Creating_twice_for_one_order_returns_the_same_shipment()
    {
        var book = new ShipmentBook();
        var orderId = Guid.NewGuid();

        var first = book.Create(orderId, "domestic", 2);
        var second = book.Create(orderId, "domestic", 2);

        Assert.Equal(ShipmentOutcome.Created, first.Outcome);
        Assert.Equal(ShipmentOutcome.AlreadyCreated, second.Outcome);
        Assert.Equal(first.Shipment!.ShipmentId, second.Shipment!.ShipmentId);
    }

    [Fact]
    public void Restricted_zone_is_not_serviceable()
    {
        Assert.Equal(ShipmentOutcome.ZoneNotServiceable, new ShipmentBook().Create(Guid.NewGuid(), "restricted", 1).Outcome);
    }
}
