# Builds the container image of one .NET service.
# The SERVICE build argument selects the project (for example OrderService), so all five
# services share this file. platformctl passes the pinned base images from deploy/versions.yaml.
ARG SDK_IMAGE=mcr.microsoft.com/dotnet/sdk:10.0
ARG RUNTIME_IMAGE=mcr.microsoft.com/dotnet/aspnet:10.0-noble-chiseled

FROM ${SDK_IMAGE} AS build
ARG SERVICE
ARG TARGETARCH
WORKDIR /source

# Restore first: this layer stays cached until a project or package version changes.
COPY global.json Directory.Build.props Directory.Packages.props ./
COPY src/ServiceDefaults/ServiceDefaults.csproj src/ServiceDefaults/
COPY src/${SERVICE}/${SERVICE}.csproj src/${SERVICE}/
RUN dotnet restore src/${SERVICE}/${SERVICE}.csproj -a ${TARGETARCH}

COPY src/ServiceDefaults/ src/ServiceDefaults/
COPY src/${SERVICE}/ src/${SERVICE}/
# The native launcher is renamed to a fixed name, because the runtime image has no shell
# that could expand the service name in the entry point.
RUN dotnet publish src/${SERVICE}/${SERVICE}.csproj -c Release -a ${TARGETARCH} --no-restore --self-contained false -o /app \
    && mv /app/${SERVICE} /app/service

FROM ${RUNTIME_IMAGE}
WORKDIR /app
COPY --from=build /app .
# The chiseled runtime image contains no shell or package manager and runs as the non-root "app" user.
USER app
ENTRYPOINT ["/app/service"]
