// The risk rules FraudService applies to an order.
namespace FraudService.Domain;

public sealed record RiskDecision(bool Approved, string Rule, int RiskScore);

public sealed class RiskOptions
{
    public const string SectionName = "Risk";

    public decimal MaxOrderAmount { get; set; } = 5000m;

    public int MaxItemCount { get; set; } = 60;
}

// Deterministic risk rules. They are simple on purpose: the platform needs a real, repeatable
// business rejection, not a realistic fraud model. The service keeps no state, which is why it
// can run several replicas.
public static class RiskRules
{
    public static RiskDecision Evaluate(decimal totalAmount, int itemCount, RiskOptions options)
    {
        if (totalAmount > options.MaxOrderAmount)
        {
            return new RiskDecision(false, "amount_threshold", 90);
        }

        if (itemCount > options.MaxItemCount)
        {
            return new RiskDecision(false, "quantity_threshold", 80);
        }

        var score = (int)Math.Min(79, totalAmount / options.MaxOrderAmount * 70 + itemCount / (decimal)options.MaxItemCount * 9);
        return new RiskDecision(true, "within_limits", score);
    }
}
