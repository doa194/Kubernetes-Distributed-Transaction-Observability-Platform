// Runs one order transaction across the four dependencies.
using System.Diagnostics;
using Microsoft.Extensions.Options;
using OrderService.Domain;
using OrderService.Infrastructure;
using OrderService.Infrastructure.Dependencies;
using OrderService.Infrastructure.Telemetry;

namespace OrderService.Features.CreateOrder;

public sealed class TransactionOptions
{
    public const string SectionName = "Transaction";

    // Covers the slowest failure path (both parallel calls, all payment attempts and every undo call).
    public TimeSpan Budget { get; set; } = TimeSpan.FromSeconds(12);
}

// Orchestrator of the distributed order transaction.
// Flow: Inventory and Fraud in parallel -> Payment -> Shipping -> Completed.
// On failure the order moves to the matching failed state and earlier steps are undone in
// reverse order. The transaction runs on its own time budget instead of the HTTP request's
// cancellation token, so a client that disconnects cannot leave an order half-processed.
public sealed class OrderTransactionCoordinator(
    IInventoryGateway inventory,
    IFraudGateway fraud,
    IPaymentGateway payment,
    IShippingGateway shipping,
    InMemoryOrderRepository repository,
    TimeProvider timeProvider,
    IOptions<TransactionOptions> options,
    ILogger<OrderTransactionCoordinator> logger)
{
    public async Task<OrderSnapshot> ExecuteAsync(Order order)
    {
        repository.Save(order.ToSnapshot());
        using var transaction = OrderTelemetry.StartTransaction(order);
        using var budget = new CancellationTokenSource(options.Value.Budget, timeProvider);

        var failure = await RunStagesAsync(order, transaction, budget.Token);
        if (failure is not null)
        {
            await CompensateAsync(order, failure, transaction);
        }

        var snapshot = order.ToSnapshot();
        repository.Save(snapshot);
        logger.LogInformation("Order {OrderId} finished in state {OrderState}", order.Id, order.State);
        return snapshot;
    }

    private async Task<OrderFailure?> RunStagesAsync(Order order, Activity? transaction, CancellationToken cancellationToken)
    {
        // The two checks are independent, so they run concurrently; the order waits for both.
        var reservation = inventory.ReserveAsync(order, cancellationToken);
        var evaluation = fraud.EvaluateAsync(order, cancellationToken);
        await Task.WhenAll(reservation, evaluation);

        var fanOutFailure = FanOutResolution.Resolve(await reservation, await evaluation);
        if (fanOutFailure is not null)
        {
            return Fail(order, fanOutFailure, transaction);
        }

        Transition(order, OrderState.InventoryReserved, transaction);

        var authorization = await payment.AuthorizeAsync(order, cancellationToken);
        if (!authorization.Succeeded)
        {
            return Fail(order, new OrderFailure(FailureStage.Payment, authorization.Kind!.Value, authorization.Reason), transaction);
        }

        Transition(order, OrderState.PaymentAuthorized, transaction);

        var shipment = await shipping.CreateAsync(order, cancellationToken);
        if (!shipment.Succeeded)
        {
            return Fail(order, new OrderFailure(FailureStage.Shipping, shipment.Kind!.Value, shipment.Reason), transaction);
        }

        Transition(order, OrderState.ShipmentCreated, transaction);
        Transition(order, OrderState.Completed, transaction);
        return null;
    }

    private async Task CompensateAsync(Order order, OrderFailure failure, Activity? transaction)
    {
        foreach (var action in CompensationPlan.For(failure))
        {
            var result = action switch
            {
                CompensationAction.VoidPayment => await payment.VoidAsync(order.Id),
                CompensationAction.ReleaseInventory => await inventory.ReleaseAsync(order.Id),
                _ => throw new InvalidOperationException($"Unknown compensation action {action}"),
            };

            // A failed undo does not change the order state; it is recorded so operators can see
            // and repair it. A durable retry of undo steps is a production concern.
            var step = new CompensationStepResult(
                action,
                result.Succeeded ? CompensationStatus.Succeeded : CompensationStatus.Failed,
                result.Succeeded ? "done" : $"{FailureNames.Kind(result.Kind!.Value)}: {result.Reason}");
            order.RecordCompensation(step);
            OrderTelemetry.RecordCompensation(transaction, step);
            if (step.Status == CompensationStatus.Failed)
            {
                logger.LogWarning("Compensation {Action} failed for order {OrderId}: {Detail}", action, order.Id, step.Detail);
            }
        }
    }

    private OrderFailure Fail(Order order, OrderFailure failure, Activity? transaction)
    {
        OrderTelemetry.RecordTransition(transaction, order.Fail(failure, timeProvider.GetUtcNow()));
        OrderTelemetry.RecordFailure(transaction, failure);
        return failure;
    }

    private void Transition(Order order, OrderState target, Activity? transaction) =>
        OrderTelemetry.RecordTransition(transaction, order.TransitionTo(target, timeProvider.GetUtcNow()));
}
