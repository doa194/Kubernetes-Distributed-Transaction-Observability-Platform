"""Creates, inspects and deletes the local kind cluster and loads images into its nodes."""

from __future__ import annotations

from txplatform import kube, paths, tools, versions


def cluster_exists() -> bool:
    result = tools.run("kind", "get", "clusters", check=False)
    return kube.CLUSTER_NAME in result.stdout.split()


def create_cluster() -> None:
    manifest = versions.load()
    tools.run(
        "kind", "create", "cluster",
        "--name", kube.CLUSTER_NAME,
        "--image", manifest.kubernetes.nodeImage,
        "--config", str(paths.deploy_dir() / "kind" / "cluster.yaml"),
        "--wait", "180s",
        timeout=900,
    )
    kube.reset_clients()


def delete_cluster() -> None:
    tools.run("kind", "delete", "cluster", "--name", kube.CLUSTER_NAME, timeout=600)
    kube.reset_clients()


def node_containers() -> list[str]:
    result = tools.run("kind", "get", "nodes", "--name", kube.CLUSTER_NAME, check=False)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def load_image(image: str) -> None:
    """Copies a local Docker image into every node; kind skips nodes that already have it."""
    tools.run("kind", "load", "docker-image", image, "--name", kube.CLUSTER_NAME, timeout=900)


def host_port_bindings() -> list[str]:
    """Returns `containerPort/proto -> hostIP:hostPort` lines of the control-plane container."""
    result = tools.run("docker", "port", f"{kube.CLUSTER_NAME}-control-plane", check=False)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
