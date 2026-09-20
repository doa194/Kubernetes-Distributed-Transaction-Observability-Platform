"""Loads and validates the scenario definitions in scenarios/*.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from txplatform import paths
from txplatform.scenarios.schema import Scenario


class CatalogError(RuntimeError):
    """A scenario file is invalid or missing."""


def parse(text: str, source: str = "<text>") -> Scenario:
    try:
        return Scenario.model_validate(yaml.safe_load(text))
    except ValidationError as error:
        raise CatalogError(f"{source} is invalid:\n{error}") from error


def load_all(directory: Path | None = None) -> dict[str, Scenario]:
    directory = directory or paths.scenarios_dir()
    scenarios: dict[str, Scenario] = {}
    for file in sorted(directory.glob("*.yaml")):
        scenario = parse(file.read_text(encoding="utf-8"), file.name)
        if scenario.id != file.stem:
            raise CatalogError(f"{file.name} declares id '{scenario.id}'; the file name must match the id")
        scenarios[scenario.id] = scenario
    return scenarios


def get(scenario_id: str) -> Scenario:
    scenarios = load_all()
    if scenario_id not in scenarios:
        raise CatalogError(f"unknown scenario '{scenario_id}'; available: {', '.join(scenarios)}")
    return scenarios[scenario_id]
