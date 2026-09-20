namespace ServiceDefaults.Hosting;

// Ports a service listens on.
// Business traffic (the service API) and management traffic (health checks, fault controls)
// use different ports, so a Kubernetes Service or a Kong route that targets the business port
// can never reach management endpoints. Setting both ports to 0 turns separation off; in-memory
// test hosts do this because they have no real network ports.
public sealed class PortOptions
{
    public const string SectionName = "Ports";

    public int Business { get; set; } = 8080;

    public int Management { get; set; } = 8081;

    public bool SeparationEnabled => Business != 0 || Management != 0;
}
