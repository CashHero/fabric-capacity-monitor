"""Optional user configuration.

Everything the tool needs is discovered from the signed-in identity; this file exists
only for convenience (a default capacity, an alias, a rate override), never as a
requirement.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "fabric-capacity-monitor" / "config.toml"


def load(path: Path | None = None) -> dict[str, Any]:
    """Load config from ``path``, else the default location, else return empty."""
    target = path or default_config_path()
    if not target.exists():
        if path is not None:
            raise FileNotFoundError(f"config file not found: {target}")
        return {}
    with target.open("rb") as handle:
        return tomllib.load(handle)


def resolve_capacity_name(config: dict[str, Any], requested: str | None) -> str | None:
    """Expand an alias from ``[aliases]``, or fall back to ``default_capacity``."""
    if requested:
        aliases = config.get("aliases", {})
        return str(aliases.get(requested, requested))
    default = config.get("default_capacity")
    return str(default) if default else None
