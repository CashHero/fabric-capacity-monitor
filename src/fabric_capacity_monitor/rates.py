"""Consumption rates, loaded from ``rates.toml`` and overridable by user config."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PACKAGED = Path(__file__).with_name("rates.toml")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class Rates:
    """Fabric consumption rates. Every field mirrors a table in ``rates.toml``."""

    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def load(cls, overrides: dict[str, Any] | None = None) -> Rates:
        with _PACKAGED.open("rb") as handle:
            data = tomllib.load(handle)
        if overrides:
            data = _deep_merge(data, overrides)
        return cls(raw=data)

    @property
    def as_of(self) -> str:
        return str(self.raw["meta"]["as_of"])

    # -- Spark ---------------------------------------------------------------
    @property
    def vcores_per_cu(self) -> float:
        return float(self.raw["spark"]["vcores_per_cu"])

    def node_vcores(self, node_size: str | None) -> int | None:
        """vCores for a pool node size, or None when the size is unrecognised."""
        table = self.raw["spark"]["node_vcores"]
        if node_size is None:
            return None
        for name, cores in table.items():
            if name.lower() == node_size.lower():
                return int(cores)
        return None

    # -- Pipelines -----------------------------------------------------------
    @property
    def orchestration_cu_seconds_per_activity(self) -> float:
        return float(self.raw["pipeline"]["orchestration_cu_hours_per_activity_run"]) * 3600.0

    # -- Dataflow Gen2 -------------------------------------------------------
    @property
    def dataflow(self) -> dict[str, Any]:
        return self.raw["dataflow_gen2"]

    # -- Smoothing -----------------------------------------------------------
    @property
    def window_seconds(self) -> int:
        return int(self.raw["smoothing"]["window_seconds"])

    @property
    def background_windows(self) -> int:
        return int(self.raw["smoothing"]["background_windows"])

    @property
    def interactive_delay_windows(self) -> int:
        return int(self.raw["smoothing"]["interactive_delay_windows"])

    @property
    def interactive_reject_windows(self) -> int:
        return int(self.raw["smoothing"]["interactive_reject_windows"])
