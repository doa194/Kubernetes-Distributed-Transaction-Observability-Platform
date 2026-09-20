// The prices OrderService knows, loaded from seed configuration.
namespace OrderService.Domain;

public sealed class CatalogItem
{
    public string Sku { get; set; } = string.Empty;

    public decimal UnitPrice { get; set; }
}

// Prices known to OrderService. Pricing is an order concern, while stock belongs to
// InventoryService. Deployments can replace the default items through the seed ConfigMap.
public sealed class CatalogOptions
{
    public const string SectionName = "Catalog";

    public string Currency { get; set; } = "EUR";

    public List<CatalogItem> Items { get; set; } = [];
}

public sealed class PriceCatalog
{
    private static readonly CatalogItem[] DefaultItems =
    [
        new() { Sku = "SKU-1001", UnitPrice = 12.50m },
        new() { Sku = "SKU-1002", UnitPrice = 49.90m },
        new() { Sku = "SKU-2001", UnitPrice = 950.00m },
        new() { Sku = "SKU-9000", UnitPrice = 5.00m },
    ];

    private readonly Dictionary<string, decimal> prices;

    public PriceCatalog(CatalogOptions options)
    {
        Currency = options.Currency;
        var items = options.Items.Count > 0 ? options.Items : [.. DefaultItems];
        prices = items.ToDictionary(item => item.Sku, item => item.UnitPrice, StringComparer.Ordinal);
    }

    public string Currency { get; }

    public bool TryGetPrice(string sku, out decimal price) => prices.TryGetValue(sku, out price);
}
