using Microsoft.AspNetCore.Http;

namespace ServiceDefaults.Hosting;

// Decides which port may serve a request path.
// The decision uses the real local port of the TCP connection and never the Host header,
// because any caller can write an arbitrary port into the Host header.
public static class PortSeparation
{
    private static readonly PathString[] ManagementPaths = ["/health", "/internal"];

    public static bool IsManagementPath(PathString path) =>
        ManagementPaths.Any(prefix => path.StartsWithSegments(prefix, StringComparison.OrdinalIgnoreCase));

    public static bool IsAllowed(PathString path, int localPort, PortOptions ports)
    {
        if (!ports.SeparationEnabled)
        {
            return true;
        }

        var expectedPort = IsManagementPath(path) ? ports.Management : ports.Business;
        return localPort == expectedPort;
    }
}
