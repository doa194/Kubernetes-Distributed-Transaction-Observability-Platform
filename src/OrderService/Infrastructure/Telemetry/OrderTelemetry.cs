using System.Diagnostics;
using OrderService.Domain;
using ServiceDefaults.Telemetry;

namespace OrderService.Infrastructure.Telemetry;

// Business spans and events of the order transaction.
// The transaction has one span; each dependency call gets a "stage" span that groups its HTTP
// attempts. State changes are events on the transaction span rather than extra spans, which keeps
// a successful order at sixteen spans (Kong's two included) while still showing every step.
public static class OrderTelemetry
{
    public const string SourceName = "OrderService.Transaction";

    public static readonly ActivitySource Source = new(SourceName);

    public static Activity? StartTransaction(Order order)
    {
        var activity = Source.StartActivity("order.transaction", ActivityKind.Internal);
        activity?.SetTag(TelemetryConventions.OrderId, order.Id.ToString());
        activity?.SetTag("order.item_count", order.ItemCount);
        return activity;
    }

    public static Activity? StartStage(string stageName, Guid orderId)
    {
        var activity = Source.StartActivity(stageName, ActivityKind.Internal);
        activity?.SetTag(TelemetryConventions.OrderId, orderId.ToString());
        return activity;
    }

    public static void RecordTransition(Activity? transaction, StateTransition transition)
    {
        transaction?.AddEvent(new ActivityEvent(TelemetryConventions.EventStateChanged, tags: new ActivityTagsCollection
        {
            ["order.state.from"] = transition.From.ToString(),
            ["order.state.to"] = transition.To.ToString(),
        }));
        transaction?.SetTag(TelemetryConventions.OrderState, transition.To.ToString());
    }

    public static void RecordStageResult(Activity? stage, StageResult result)
    {
        if (stage is null || result.Succeeded)
        {
            return;
        }

        stage.SetTag(TelemetryConventions.FailureKind, FailureNames.Kind(result.Kind!.Value));
        stage.SetTag(TelemetryConventions.FailureReason, result.Reason);
        if (result.Kind != FailureKind.BusinessRejection)
        {
            stage.SetStatus(ActivityStatusCode.Error, result.Reason);
            stage.SetTag("error.type", FailureNames.Kind(result.Kind!.Value));
        }
    }

    public static void RecordFailure(Activity? transaction, OrderFailure failure)
    {
        if (transaction is null)
        {
            return;
        }

        transaction.SetTag(TelemetryConventions.FailureStage, FailureNames.Stage(failure.Stage));
        transaction.SetTag(TelemetryConventions.FailureKind, FailureNames.Kind(failure.Kind));
        transaction.SetTag(TelemetryConventions.FailureReason, failure.Reason);
        if (failure.Kind != FailureKind.BusinessRejection)
        {
            transaction.SetStatus(ActivityStatusCode.Error, $"{FailureNames.Stage(failure.Stage)} {FailureNames.Kind(failure.Kind)}");
            transaction.SetTag("error.type", FailureNames.Kind(failure.Kind));
        }
    }

    // Called by the retry strategy before it waits and tries again. The current span is the
    // stage span, so the event shows exactly which call was retried and how long it waited.
    public static void RecordRetry(int failedAttemptNumber, TimeSpan delay, string reason)
    {
        var stage = Activity.Current;
        if (stage is null)
        {
            return;
        }

        stage.AddEvent(new ActivityEvent(TelemetryConventions.EventRetry, tags: new ActivityTagsCollection
        {
            ["retry.failed_attempt"] = failedAttemptNumber,
            ["retry.delay_ms"] = (long)delay.TotalMilliseconds,
            ["retry.reason"] = reason,
        }));
        stage.SetTag(TelemetryConventions.RetryCount, failedAttemptNumber);
    }

    public static void RecordCircuitStateChange(string dependency, string state) =>
        Activity.Current?.AddEvent(new ActivityEvent(TelemetryConventions.EventCircuitStateChanged, tags: new ActivityTagsCollection
        {
            ["circuit.dependency"] = dependency,
            ["circuit.state"] = state,
        }));

    public static void RecordCompensation(Activity? transaction, CompensationStepResult result)
    {
        var name = result.Status == CompensationStatus.Succeeded
            ? TelemetryConventions.EventCompensationCompleted
            : TelemetryConventions.EventCompensationFailed;
        transaction?.AddEvent(new ActivityEvent(name, tags: new ActivityTagsCollection
        {
            ["compensation.action"] = result.Action.ToString(),
            ["compensation.detail"] = result.Detail,
        }));
    }
}
