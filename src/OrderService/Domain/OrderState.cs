using System.Text.Json.Serialization;

namespace OrderService.Domain;

// Lifecycle of an order. The first five states form the successful path; each "Failed" state
// records the stage where the transaction stopped. Failed states and Completed are final.
public enum OrderState
{
    Pending,
    InventoryReserved,
    PaymentAuthorized,
    ShipmentCreated,
    Completed,
    InventoryFailed,
    FraudFailed,
    PaymentFailed,
    ShippingFailed,
}

// Where a transaction failed.
public enum FailureStage
{
    [JsonStringEnumMemberName("inventory")]
    Inventory,
    [JsonStringEnumMemberName("fraud")]
    Fraud,
    [JsonStringEnumMemberName("payment")]
    Payment,
    [JsonStringEnumMemberName("shipping")]
    Shipping,
}

// Why a transaction failed. Only BusinessRejection is an expected business outcome; the
// other kinds are technical failures and are marked as errors in traces.
public enum FailureKind
{
    [JsonStringEnumMemberName("business_rejection")]
    BusinessRejection,
    [JsonStringEnumMemberName("dependency_error")]
    DependencyError,
    [JsonStringEnumMemberName("timeout")]
    Timeout,
    [JsonStringEnumMemberName("unavailable")]
    Unavailable,
}

public static class FailureNames
{
    public static string Kind(FailureKind kind) => kind switch
    {
        FailureKind.BusinessRejection => "business_rejection",
        FailureKind.DependencyError => "dependency_error",
        FailureKind.Timeout => "timeout",
        FailureKind.Unavailable => "unavailable",
        _ => kind.ToString(),
    };

    public static string Stage(FailureStage stage) => stage.ToString().ToLowerInvariant();
}
