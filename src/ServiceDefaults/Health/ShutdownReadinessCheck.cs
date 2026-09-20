using Microsoft.Extensions.Diagnostics.HealthChecks;
using Microsoft.Extensions.Hosting;

namespace ServiceDefaults.Health;

// Readiness turns false as soon as the host begins shutting down.
// Kubernetes then removes the pod from Service endpoints while in-flight requests still finish,
// which is what lets a rolling restart complete without failed requests.
internal sealed class ShutdownReadinessCheck(IHostApplicationLifetime lifetime) : IHealthCheck
{
    public Task<HealthCheckResult> CheckHealthAsync(HealthCheckContext context, CancellationToken cancellationToken = default) =>
        Task.FromResult(lifetime.ApplicationStopping.IsCancellationRequested
            ? HealthCheckResult.Unhealthy("the service is shutting down")
            : HealthCheckResult.Healthy());
}
