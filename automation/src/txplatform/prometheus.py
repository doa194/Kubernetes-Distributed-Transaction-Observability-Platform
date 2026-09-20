"""Client for the Prometheus HTTP API, reached through a port-forward.

Metric names produced by the OpenTelemetry Collector gained unit and "_total" suffixes in some
versions, so lookups use `metric_selector`, which matches a name with or without that suffix.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

from txplatform import kube, portforward


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float


def metric_selector(name: str, **labels: str) -> str:
    """PromQL selector matching `name` and `name_total`, optionally filtered by exact label values."""
    base = name.removesuffix("_total")
    matchers = [f'__name__=~"{base}(_total)?"'] + [f'{key}="{value}"' for key, value in labels.items()]
    return "{" + ",".join(matchers) + "}"


def parse_vector(payload: dict) -> list[Sample]:
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload.get('error')}")
    result = payload["data"]["result"]
    return [Sample(dict(item["metric"]), float(item["value"][1])) for item in result]


class PrometheusClient:
    def __init__(self, base_url: str) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=30)

    def close(self) -> None:
        self._http.close()

    def query(self, expression: str) -> list[Sample]:
        response = self._http.get("/api/v1/query", params={"query": expression})
        response.raise_for_status()
        return parse_vector(response.json())

    def total(self, expression: str) -> float:
        """Sum of all series of an instant query; 0 when nothing matches."""
        return sum(sample.value for sample in self.query(expression))

    def targets(self) -> list[dict]:
        response = self._http.get("/api/v1/targets")
        response.raise_for_status()
        return response.json()["data"]["activeTargets"]

    def max_over(self, selector: str, seconds: float) -> float:
        """Highest value a gauge reached during the last `seconds`; 0 when nothing matched."""
        samples = self.query(f"max_over_time({selector}[{int(seconds)}s])")
        return max((sample.value for sample in samples), default=0.0)

    def current_label_names(self, selector: str) -> set[str]:
        """Label names of the series an instant query returns. Unlike the labels API, this ignores
        series that are no longer scraped (for example from a replaced Collector pod)."""
        return {name for sample in self.query(selector) for name in sample.labels}


@contextmanager
def connect() -> Iterator[PrometheusClient]:
    with portforward.forward(kube.NS_OBSERVABILITY, "svc/prometheus-server", 9090) as port:
        client = PrometheusClient(f"http://127.0.0.1:{port}")
        try:
            yield client
        finally:
            client.close()
