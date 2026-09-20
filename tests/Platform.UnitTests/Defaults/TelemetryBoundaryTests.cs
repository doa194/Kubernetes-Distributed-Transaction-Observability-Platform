using System.Diagnostics;
using OpenTelemetry;
using OrderService.Infrastructure.Telemetry;
using ServiceDefaults.Telemetry;

namespace Platform.UnitTests.Defaults;

// Identifier validation and the baggage allow-list decide which caller-supplied values may
// become span attributes.
public sealed class TelemetryBoundaryTests
{
    [Theory]
    [InlineData("0f8fad5b-d9cb-469f-a165-70867728950e", true)]
    [InlineData("payment-retry.run:20260917", true)]
    [InlineData("", false)]
    [InlineData("-leading-dash", false)]
    [InlineData("has space", false)]
    [InlineData("<script>", false)]
    public void Only_plain_short_identifiers_are_accepted(string value, bool valid)
    {
        Assert.Equal(valid, CorrelationBoundary.IsValidId(value));
    }

    [Fact]
    public void Missing_or_invalid_correlation_id_is_replaced_by_a_new_one()
    {
        Assert.Equal("corr-1", CorrelationBoundary.CorrelationIdOrNew("corr-1"));
        Assert.True(Guid.TryParse(CorrelationBoundary.CorrelationIdOrNew("bad value!"), out _));
        Assert.True(Guid.TryParse(CorrelationBoundary.CorrelationIdOrNew(null), out _));
    }

    [Fact]
    public void Only_allow_listed_and_bounded_baggage_becomes_span_attributes()
    {
        using var activity = new Activity("test").Start();
        Baggage.SetBaggage(TelemetryConventions.ScenarioId, "normal-order");
        Baggage.SetBaggage(TelemetryConventions.CorrelationId, new string('x', BaggageTagProcessor.MaxValueLength + 1));
        Baggage.SetBaggage("user.email", "someone@example.com");

        BaggageTagProcessor.CopyAllowedBaggage(activity);

        Assert.Equal("normal-order", activity.GetTagItem(TelemetryConventions.ScenarioId));
        Assert.Null(activity.GetTagItem(TelemetryConventions.CorrelationId));
        Assert.Null(activity.GetTagItem("user.email"));
        Baggage.ClearBaggage();
    }
}
