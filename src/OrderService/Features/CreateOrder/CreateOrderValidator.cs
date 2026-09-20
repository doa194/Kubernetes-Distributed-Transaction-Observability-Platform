// Request shape and validation rules of POST /orders.
using System.Text.RegularExpressions;
using OrderService.Domain;

namespace OrderService.Features.CreateOrder;

public sealed record CreateOrderLine(string? Sku, int Quantity);

public sealed record CreateOrderRequest(IReadOnlyList<CreateOrderLine>? Items, string? PaymentToken, string? ShippingZone);

// Rejects malformed orders before any order is created or any dependency is called.
// The API accepts no personal or card data: payment tokens are opaque references, and any
// value that looks like a card number is refused so it can never reach logs or traces.
public static partial class CreateOrderValidator
{
    public const int MaxLines = 20;
    public const int MaxQuantity = 100;
    public static readonly IReadOnlySet<string> ShippingZones = new HashSet<string>(StringComparer.Ordinal) { "domestic", "international", "restricted" };

    public static IReadOnlyDictionary<string, string[]> Validate(CreateOrderRequest? request, PriceCatalog catalog)
    {
        var errors = new Dictionary<string, List<string>>(StringComparer.Ordinal);

        void Add(string field, string message)
        {
            if (!errors.TryGetValue(field, out var messages))
            {
                errors[field] = messages = [];
            }

            messages.Add(message);
        }

        if (request is null)
        {
            Add("body", "a JSON order is required");
            return Freeze(errors);
        }

        var items = request.Items ?? [];
        if (items.Count is 0 or > MaxLines)
        {
            Add("items", $"between 1 and {MaxLines} lines are required");
        }

        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (var index = 0; index < items.Count; index++)
        {
            var line = items[index];
            if (line.Sku is null || !SkuPattern().IsMatch(line.Sku))
            {
                Add($"items[{index}].sku", "must look like SKU-1234");
            }
            else if (!catalog.TryGetPrice(line.Sku, out _))
            {
                Add($"items[{index}].sku", "is not in the catalog");
            }
            else if (!seen.Add(line.Sku))
            {
                Add($"items[{index}].sku", "appears more than once");
            }

            if (line.Quantity is < 1 or > MaxQuantity)
            {
                Add($"items[{index}].quantity", $"must be between 1 and {MaxQuantity}");
            }
        }

        if (request.PaymentToken is null || !PaymentTokenPattern().IsMatch(request.PaymentToken))
        {
            Add("paymentToken", "must be an opaque token such as tok_test_approved");
        }
        else if (CardNumberPattern().IsMatch(request.PaymentToken))
        {
            Add("paymentToken", "must not contain card numbers");
        }

        if (request.ShippingZone is null || !ShippingZones.Contains(request.ShippingZone))
        {
            Add("shippingZone", $"must be one of: {string.Join(", ", ShippingZones)}");
        }

        return Freeze(errors);
    }

    private static Dictionary<string, string[]> Freeze(Dictionary<string, List<string>> errors) =>
        errors.ToDictionary(entry => entry.Key, entry => entry.Value.ToArray(), StringComparer.Ordinal);

    [GeneratedRegex("^SKU-[0-9]{4}$")]
    private static partial Regex SkuPattern();

    [GeneratedRegex("^tok_[a-z0-9_]{3,64}$")]
    private static partial Regex PaymentTokenPattern();

    // 12 to 19 consecutive digits is the length range of payment card numbers.
    [GeneratedRegex("[0-9]{12,19}")]
    private static partial Regex CardNumberPattern();
}
