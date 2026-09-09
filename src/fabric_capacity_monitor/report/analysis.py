"""Aggregate operations and the smoothed timeline into everything a renderer needs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..collect import Collection
from ..model import Operation, Window
from ..rates import Rates
from ..smoothing import build_timeline, throttle_breaches


@dataclass
class ItemRow:
    """One row of the by-item matrix. Under high concurrency this is a *session* row."""

    workspace: str
    item_kind: str
    item_name: str
    cu_seconds: float = 0.0
    duration_seconds: float = 0.0
    queued_seconds: float = 0.0
    operations: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    users: set[str] = field(default_factory=set)
    exactness: str = "exact"
    cu_seconds_prior: float = 0.0  # same metric one week earlier, for performance delta

    @property
    def performance_delta(self) -> float | None:
        """Percent change in CU against the previous week; None when there's no baseline."""
        if self.cu_seconds_prior <= 0:
            return None
        return (self.cu_seconds - self.cu_seconds_prior) / self.cu_seconds_prior * 100.0


@dataclass
class DayRow:
    date: str
    cu_seconds: float = 0.0
    duration_seconds: float = 0.0
    queued_seconds: float = 0.0
    operations: int = 0
    failed: int = 0

    def utilization(self, daily_budget: float | None) -> float | None:
        if not daily_budget:
            return None
        return self.cu_seconds / daily_budget


@dataclass
class Analysis:
    collection: Collection
    rates: Rates
    start: datetime
    end: datetime
    windows: list[Window]
    days: list[DayRow]
    items: list[ItemRow]
    workspaces: list[ItemRow]
    breaches: dict[str, int]
    total_cu_seconds: float
    total_operations: int
    users: set[str]
    cost_rows: list[tuple[float, str, str]] | None = None
    activity_events: list[dict] = field(default_factory=list)

    @property
    def capacity(self):
        return self.collection.capacity

    @property
    def average_utilization(self) -> float | None:
        """Mean utilization across windows that carried any load.

        The app excludes inactive timepoints from its average, so a capacity that is
        idle overnight isn't reported as healthier than it is.
        """
        active = [w.utilization for w in self.windows if w.cu_seconds > 0]
        return sum(active) / len(active) if active else None

    @property
    def peak_utilization(self) -> float | None:
        return max((w.utilization for w in self.windows), default=None)

    @property
    def peak_window(self) -> Window | None:
        if not self.windows:
            return None
        return max(self.windows, key=lambda w: w.utilization)


def _bucket(operations: list[Operation], key) -> dict:
    grouped: dict = defaultdict(list)
    for operation in operations:
        grouped[key(operation)].append(operation)
    return grouped


def _rows(grouped: dict, prior: dict[tuple, float]) -> list[ItemRow]:
    rows: list[ItemRow] = []
    for (workspace, kind, name), operations in grouped.items():
        row = ItemRow(workspace=workspace, item_kind=kind, item_name=name)
        for operation in operations:
            row.cu_seconds += operation.cu_seconds
            row.duration_seconds += operation.duration_seconds
            row.queued_seconds += operation.queued_seconds
            row.operations += 1
            row.succeeded += 1 if operation.succeeded else 0
            row.failed += 1 if operation.failed else 0
            row.cancelled += 1 if operation.cancelled else 0
            if operation.user:
                row.users.add(operation.user)
            if operation.exactness != "exact":
                row.exactness = "estimated"
        row.cu_seconds_prior = prior.get((workspace, kind, name), 0.0)
        rows.append(row)
    return sorted(rows, key=lambda r: -r.cu_seconds)


def analyse(
    collection: Collection,
    rates: Rates,
    start: datetime,
    end: datetime,
    *,
    cost_rows: list[tuple[float, str, str]] | None = None,
    activity_events: list[dict] | None = None,
) -> Analysis:
    operations = [op for op in collection.operations if op.end and start <= op.end <= end]
    base_cu = collection.capacity.base_cu or 0.0
    windows = build_timeline(operations, start, end, base_cu, rates) if base_cu else []

    days_map: dict[str, DayRow] = {}
    for operation in operations:
        key = operation.end.strftime("%Y-%m-%d")
        row = days_map.setdefault(key, DayRow(date=key))
        row.cu_seconds += operation.cu_seconds
        row.duration_seconds += operation.duration_seconds
        row.queued_seconds += operation.queued_seconds
        row.operations += 1
        row.failed += 1 if operation.failed else 0

    # Performance delta compares the reported window against the one a week before it.
    prior_start = start - timedelta(days=7)
    prior_ops = [
        op for op in collection.operations if op.end and prior_start <= op.end < start
    ]
    prior_cu: dict[tuple, float] = defaultdict(float)
    for operation in prior_ops:
        prior_cu[
            (operation.workspace_name, operation.item_kind, operation.item_name)
        ] += operation.cu_seconds

    item_groups = _bucket(
        operations, lambda op: (op.workspace_name, op.item_kind, op.item_name)
    )
    ws_groups = _bucket(operations, lambda op: (op.workspace_name, "Workspace", op.workspace_name))
    ws_prior: dict[tuple, float] = defaultdict(float)
    for operation in prior_ops:
        ws_prior[(operation.workspace_name, "Workspace", operation.workspace_name)] += (
            operation.cu_seconds
        )

    return Analysis(
        collection=collection,
        rates=rates,
        start=start,
        end=end,
        windows=windows,
        days=[days_map[k] for k in sorted(days_map)],
        items=_rows(item_groups, prior_cu),
        workspaces=_rows(ws_groups, ws_prior),
        breaches=throttle_breaches(windows),
        total_cu_seconds=sum(op.cu_seconds for op in operations),
        total_operations=len(operations),
        users={op.user for op in operations if op.user},
        cost_rows=cost_rows,
        activity_events=activity_events or [],
    )
