"""Temporary operator access to cluster-internal ports through `kubectl port-forward`.

Management ports, the Jaeger Query API, Prometheus and the Collector's OTLP endpoint are never
exposed outside the cluster. Port-forwarding is the controlled way operators reach them: it
requires Kubernetes permissions and it does not pass through NetworkPolicies, because the
connection is made by the kubelet directly into the pod.
"""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from txplatform import kube, tools

_FORWARDING = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+) ->")


@contextmanager
def forward(namespace: str, target: str, remote_port: int, *, local_port: int = 0, timeout: float = 30) -> Iterator[int]:
    """Forwards a local port (random unless given) to `target` (e.g. `pod/name` or `svc/name`) and yields it."""
    mapping = f"{local_port}:{remote_port}" if local_port else f":{remote_port}"
    command = [
        str(tools.require_tool("kubectl")), "--context", kube.CONTEXT, "--namespace", namespace,
        "port-forward", target, mapping, "--address", "127.0.0.1",
    ]
    process = tools.start(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    try:
        deadline = time.monotonic() + timeout
        output: list[str] = []
        local_port: int | None = None
        while local_port is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.poll() is not None:
                raise RuntimeError(f"port-forward to {namespace}/{target}:{remote_port} failed: {''.join(output)[-500:]}")
            try:
                line = lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            output.append(line)
            match = _FORWARDING.search(line)
            if match:
                local_port = int(match.group(1))
        yield local_port
    finally:
        tools.terminate_tree(process)
