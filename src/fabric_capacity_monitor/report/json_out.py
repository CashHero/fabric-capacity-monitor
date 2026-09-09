"""Machine-readable output: the normalised records plus the derived timeline."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

from .analysis import Analysis


def _encode(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def render(analysis: Analysis, *, include_windows: bool = True) -> str:
    capacity = analysis.capacity
    payload = {
        "capacity": {
            "id": capacity.id,
            "name": capacity.name,
            "sku": capacity.sku,
            "region": capacity.region,
            "state": capacity.state,
            "base_cu": capacity.base_cu,
            "daily_budget_cu_seconds": capacity.daily_budget_cu_seconds(),
            "workspaces": [w.get("displayName") for w in capacity.workspaces],
        },
        "range": {
            "start": analysis.start,
            "end": analysis.end,
            # "operations" also carries the extra week fetched to baseline the
            # week-over-week delta, so it can start before "start".
            "operations_from": min(
                (op.end for op in analysis.collection.operations if op.end),
                default=analysis.start,
            ),
        },
        "rates_as_of": analysis.rates.as_of,
        "summary": {
            "average_utilization": analysis.average_utilization,
            "peak_utilization": analysis.peak_utilization,
            "total_cu_seconds": analysis.total_cu_seconds,
            "total_operations": analysis.total_operations,
            "distinct_users": len(analysis.users),
            "throttle_breaches": analysis.breaches,
        },
        "days": [asdict(day) for day in analysis.days],
        "items": [
            {**asdict(row), "users": sorted(row.users), "performance_delta": row.performance_delta}
            for row in analysis.items
        ],
        "operations": [asdict(op) for op in analysis.collection.operations],
        "warnings": analysis.collection.warnings,
        "unaccounted": analysis.collection.unaccounted,
    }
    if include_windows:
        payload["windows"] = [asdict(w) for w in analysis.windows]
    return json.dumps(payload, indent=2, default=_encode)
