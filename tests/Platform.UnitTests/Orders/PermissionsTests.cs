using System.Security.Claims;
using OrderService.Infrastructure.Auth;

namespace Platform.UnitTests.Orders;

// Only callers holding traces.debug may force their traces to be kept.
public sealed class PermissionsTests
{
    private static ClaimsPrincipal User(params string[] permissions) =>
        new(new ClaimsIdentity(permissions.Select(p => new Claim(Permissions.ClaimType, p)), "test"));

    [Theory]
    [InlineData("true", true)]
    [InlineData("TRUE", true)]
    [InlineData("false", false)]
    [InlineData(null, false)]
    public void Debug_header_is_honored_only_with_the_debug_permission(string? header, bool expected)
    {
        Assert.Equal(expected, Permissions.MayRequestDebugTrace(User("orders.write", "traces.debug"), header));
        Assert.False(Permissions.MayRequestDebugTrace(User("orders.write"), header));
    }
}
