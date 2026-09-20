// HTTP API of PaymentService: authorize a payment once, void it, and read the ledger.
using System.Diagnostics;
using System.Text.Json;
using PaymentService.Domain;
using PaymentService.Infrastructure;
using ServiceDefaults.Faults;
using ServiceDefaults.Telemetry;

namespace PaymentService.Features;

public sealed record AuthorizePaymentRequest(Guid OrderId, decimal Amount, string? Currency, string? PaymentToken);

public sealed record VoidPaymentRequest(Guid OrderId);

// Vertical slices of PaymentService: authorize a payment idempotently, void it, and let
// operators read the ledger to prove no order was charged twice.
public static class PaymentEndpoints
{
    public const string AuthorizeOperation = "payment.authorize";
    public const string VoidOperation = "payment.void";
    private const string ProblemTypeBase = "https://txplatform.local/problems/";

    public static IServiceCollection AddPaymentFeatures(this IServiceCollection services)
    {
        services.AddSingleton<IdempotencyStore>();
        services.AddSingleton<PaymentLedger>();
        return services;
    }

    public static IEndpointRouteBuilder MapPaymentEndpoints(this IEndpointRouteBuilder endpoints)
    {
        endpoints.MapPost("/payments/authorizations", AuthorizeAsync);
        endpoints.MapPost("/payments/voids", VoidAsync);
        // Management port only (see port separation): used by operators and scenario verification.
        endpoints.MapGet("/internal/payments/{orderId:guid}", (Guid orderId, PaymentLedger ledger) => Results.Ok(ledger.Summarize(orderId)));
        return endpoints;
    }

    private static async Task<IResult> AuthorizeAsync(
        AuthorizePaymentRequest request,
        HttpContext http,
        IdempotencyStore idempotency,
        PaymentLedger ledger,
        FaultInjector faults)
    {
        var activity = Activity.Current;
        activity?.SetTag(TelemetryConventions.OrderId, request.OrderId.ToString());
        if (http.Request.Headers["X-Attempt"].ToString() is { Length: > 0 } attempt && int.TryParse(attempt, out var attemptNumber))
        {
            activity?.SetTag(TelemetryConventions.PaymentAttempt, attemptNumber);
        }

        var key = http.Request.Headers["Idempotency-Key"].ToString();
        if (string.IsNullOrWhiteSpace(key) || key.Length > 128)
        {
            return Results.Problem(statusCode: StatusCodes.Status400BadRequest, title: "Idempotency-Key header is required", type: ProblemTypeBase + "idempotency-key-required");
        }

        if (request.OrderId == Guid.Empty || request.Amount <= 0 || string.IsNullOrWhiteSpace(request.Currency) || string.IsNullOrWhiteSpace(request.PaymentToken))
        {
            return Results.ValidationProblem(new Dictionary<string, string[]> { ["payment"] = ["orderId, a positive amount, currency and paymentToken are required"] });
        }

        var fingerprint = IdempotencyRules.Fingerprint(request.OrderId, request.Amount, request.Currency, request.PaymentToken);
        var (decision, existing) = idempotency.TryBegin(key, fingerprint);
        activity?.SetTag(TelemetryConventions.PaymentIdempotentReplay, decision == IdempotencyDecision.ReplayStored);
        switch (decision)
        {
            case IdempotencyDecision.ReplayStored:
                // A retry of a request that already finished: answer exactly as before, change nothing.
                http.Response.Headers["Idempotent-Replayed"] = "true";
                activity?.AddEvent(new ActivityEvent("payment.idempotent_replay"));
                return Results.Text(existing!.Response!.Body, "application/json", statusCode: existing.Response.StatusCode);
            case IdempotencyDecision.RejectInProgress:
                http.Response.Headers.RetryAfter = "1";
                return Results.Problem(statusCode: StatusCodes.Status409Conflict, title: "A request with this Idempotency-Key is still being processed", type: ProblemTypeBase + "idempotency-in-progress");
            case IdempotencyDecision.RejectFingerprintMismatch:
                return Results.Problem(statusCode: StatusCodes.Status422UnprocessableEntity, title: "Idempotency-Key was used with a different request", type: ProblemTypeBase + "idempotency-key-mismatch");
        }

        var cancellationToken = http.RequestAborted;
        var completed = false;
        try
        {
            var fault = await faults.ApplyAsync(AuthorizeOperation, FaultPhase.BeforeEffect, key, cancellationToken);
            if (fault.Outcome == FaultOutcome.Fail)
            {
                return Results.Problem(statusCode: fault.StatusCode, title: "Injected fault", type: ProblemTypeBase + "injected-fault");
            }

            var paymentDecision = fault.Outcome == FaultOutcome.Reject
                ? new PaymentDecision(false, "simulated_decline")
                : PaymentRules.Decide(request.PaymentToken);

            StoredResponse response;
            if (paymentDecision.Approved)
            {
                var authorization = ledger.Authorize(request.OrderId, request.Amount, request.Currency);
                activity?.AddEvent(new ActivityEvent("payment.authorization_recorded"));
                response = new StoredResponse(StatusCodes.Status201Created, JsonSerializer.Serialize(new
                {
                    authorizationId = authorization.AuthorizationId,
                    orderId = request.OrderId,
                    amount = request.Amount,
                    currency = request.Currency,
                    status = "authorized",
                }, JsonSerializerOptions.Web));
            }
            else
            {
                response = new StoredResponse(StatusCodes.Status422UnprocessableEntity, JsonSerializer.Serialize(new
                {
                    type = ProblemTypeBase + "payment-declined",
                    title = "Payment declined",
                    status = StatusCodes.Status422UnprocessableEntity,
                    detail = paymentDecision.Reason,
                }, JsonSerializerOptions.Web));
            }

            activity?.SetTag("payment.decision", paymentDecision.Approved ? "authorized" : "declined");
            // The outcome is stored before any delayed reply, so a retry after a lost response gets this answer.
            idempotency.Complete(key, response);
            completed = true;

            await faults.ApplyAsync(AuthorizeOperation, FaultPhase.AfterEffect, key, cancellationToken);
            return Results.Text(response.Body, response.StatusCode == StatusCodes.Status201Created ? "application/json" : "application/problem+json", statusCode: response.StatusCode);
        }
        finally
        {
            if (!completed)
            {
                // Nothing was charged, so a later attempt with the same key may start again.
                idempotency.Abandon(key);
            }
        }
    }

    private static async Task<IResult> VoidAsync(VoidPaymentRequest request, PaymentLedger ledger, FaultInjector faults, CancellationToken cancellationToken)
    {
        Activity.Current?.SetTag(TelemetryConventions.OrderId, request.OrderId.ToString());
        var fault = await faults.ApplyAsync(VoidOperation, FaultPhase.BeforeEffect, request.OrderId.ToString(), cancellationToken);
        if (fault.Outcome == FaultOutcome.Fail)
        {
            return Results.Problem(statusCode: fault.StatusCode, title: "Injected fault", type: ProblemTypeBase + "injected-fault");
        }

        var voided = ledger.Void(request.OrderId);
        Activity.Current?.SetTag("payment.void.result", voided ? "voided" : "not_found");
        return Results.Ok(new { orderId = request.OrderId, status = voided ? "voided" : "not_found" });
    }
}
