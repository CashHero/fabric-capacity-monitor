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
            "sku_changes": [asdict(change) for change in capacity.sku_changes],
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
            # The same load against the current SKU throughout; drives the exit code.
            "current_sku_peak_utilization": analysis.current_peak_utilization,
            "current_sku_throttle_breaches": analysis.current_breaches,
            "at_risk": analysis.at_risk,
        },
        # The next 24 h on the current SKU if nothing new runs.
        "outlook": {
            "target": analysis.target,
            "utilization_now": analysis.outlook[0].utilization if analysis.outlook else None,
            "recovery_at": analysis.recovery_at,
            "hold_off_seconds": analysis.hold_off_seconds,
            "headroom_cu_seconds": analysis.headroom_cu_seconds,
        },
        "peak_drivers": [
            {
                "workspace": row.workspace,
                "item_kind": row.item_kind,
                "item_name": row.item_name,
                "cu_seconds": row.cu_seconds,
                "operations": row.operations,
                "exactness": row.exactness,
                "utilization_points": row.cu_seconds / analysis.peak_window.budget_cu_seconds,
            }
            for row in analysis.peak_drivers
        ],
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
        payload["outlook"]["windows"] = [asdict(w) for w in analysis.outlook]
    return json.dumps(payload, indent=2, default=_encode)
