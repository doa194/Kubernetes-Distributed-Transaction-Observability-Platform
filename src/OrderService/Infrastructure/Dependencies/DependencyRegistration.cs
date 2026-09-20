using Microsoft.Extensions.Http.Resilience;
using Microsoft.Extensions.Options;
using OrderService.Infrastructure.Telemetry;
using Polly;

namespace OrderService.Infrastructure.Dependencies;

// Registers one named HttpClient per dependency profile with its own resilience pipeline.
// Strategy order, from outside to inside: stage budget -> retry -> circuit breaker -> attempt timeout.
// The attempt-number handler is added last so it runs once for every attempt.
public static class DependencyRegistration
{
    // Recycling pooled connections lets traffic spread over replicas and reach replaced pods.
    private static readonly TimeSpan PooledConnectionLifetime = TimeSpan.FromSeconds(30);

    public static IServiceCollection AddDependencyGateways(this IServiceCollection services, IConfiguration configuration)
    {
        services.Configure<DependencyOptions>(configuration.GetSection(DependencyOptions.SectionName));
        services.AddSingleton<CircuitBreakerControls>();
        services.AddTransient<AttemptNumberHandler>();

        AddClient(services, DependencyProfiles.Inventory, options => options.InventoryBaseUrl);
        AddClient(services, DependencyProfiles.InventoryCompensation, options => options.InventoryBaseUrl);
        AddClient(services, DependencyProfiles.Fraud, options => options.FraudBaseUrl);
        AddClient(services, DependencyProfiles.Payment, options => options.PaymentBaseUrl);
        AddClient(services, DependencyProfiles.PaymentCompensation, options => options.PaymentBaseUrl);
        AddClient(services, DependencyProfiles.Shipping, options => options.ShippingBaseUrl);

        services.AddSingleton<IInventoryGateway, InventoryGateway>();
        services.AddSingleton<IFraudGateway, FraudGateway>();
        services.AddSingleton<IPaymentGateway, PaymentGateway>();
        services.AddSingleton<IShippingGateway, ShippingGateway>();
        return services;
    }

    private static void AddClient(IServiceCollection services, DependencyProfile profile, Func<DependencyOptions, Uri> baseUrl)
    {
        var client = services.AddHttpClient(profile.Name, (provider, client) =>
            {
                client.BaseAddress = baseUrl(provider.GetRequiredService<IOptions<DependencyOptions>>().Value);
                // The resilience pipeline owns all timeouts; HttpClient's own 100 s default must never interfere.
                client.Timeout = Timeout.InfiniteTimeSpan;
            })
            .ConfigurePrimaryHttpMessageHandler(() => new SocketsHttpHandler { PooledConnectionLifetime = PooledConnectionLifetime });

        client.AddResilienceHandler(profile.Name, (pipeline, context) => ConfigurePipeline(pipeline, profile, context.ServiceProvider));
        client.AddHttpMessageHandler<AttemptNumberHandler>();
    }

    private static void ConfigurePipeline(ResiliencePipelineBuilder<HttpResponseMessage> pipeline, DependencyProfile profile, IServiceProvider provider)
    {
        pipeline.AddTimeout(profile.StageBudget);

        pipeline.AddRetry(new HttpRetryStrategyOptions
        {
            MaxRetryAttempts = profile.MaxAttempts - 1,
            BackoffType = DelayBackoffType.Exponential,
            UseJitter = true,
            Delay = TimeSpan.FromMilliseconds(200),
            // Predictable timing matters more than honoring Retry-After for these short local calls.
            ShouldRetryAfterHeader = false,
            ShouldHandle = args => ValueTask.FromResult(RetryClassifier.ShouldRetry(profile, CallOutcome.From(args.Outcome))),
            OnRetry = args =>
            {
                OrderTelemetry.RecordRetry(args.AttemptNumber + 1, args.RetryDelay, CallOutcome.From(args.Outcome).Describe());
                return ValueTask.CompletedTask;
            },
        });

        if (profile.UseCircuitBreaker)
        {
            pipeline.AddCircuitBreaker(new HttpCircuitBreakerStrategyOptions
            {
                FailureRatio = 0.5,
                MinimumThroughput = 10,
                SamplingDuration = TimeSpan.FromSeconds(30),
                BreakDuration = TimeSpan.FromSeconds(5),
                ManualControl = provider.GetRequiredService<CircuitBreakerControls>().For(profile.Name),
                ShouldHandle = args => ValueTask.FromResult(RetryClassifier.CountsAsBreakerFailure(CallOutcome.From(args.Outcome))),
                OnOpened = _ =>
                {
                    OrderTelemetry.RecordCircuitStateChange(profile.Name, "open");
                    return ValueTask.CompletedTask;
                },
                OnClosed = _ =>
                {
                    OrderTelemetry.RecordCircuitStateChange(profile.Name, "closed");
                    return ValueTask.CompletedTask;
                },
                OnHalfOpened = _ =>
                {
                    OrderTelemetry.RecordCircuitStateChange(profile.Name, "half_open");
                    return ValueTask.CompletedTask;
                },
            });
        }

        pipeline.AddTimeout(profile.AttemptTimeout);
    }
}
