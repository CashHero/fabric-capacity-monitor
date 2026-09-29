"""Aggregate operations and the smoothed timeline into everything a renderer needs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..collect import Collection
from ..model import Capacity, Operation, SkuChange, Window, sku_cu
from ..rates import Rates
from ..smoothing import build_timeline, throttle_breaches, window_contributions


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
    budget_cu_seconds: float | None = None  # the day's capacity, following any resize

    def utilization(self) -> float | None:
        if not self.budget_cu_seconds:
            return None
        return self.cu_seconds / self.budget_cu_seconds


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
    breaches: dict[str, int]  # as it happened, against the SKU in effect at the time
    # The same load against the current SKU throughout: the risk as the capacity stands.
    # Identical to `breaches` / `peak_utilization` unless the capacity was resized.
    current_breaches: dict[str, int]
    current_peak_utilization: float | None
    total_cu_seconds: float
    total_operations: int
    users: set[str]
    cost_rows: list[tuple[float, str, str]] | None = None
    activity_events: list[dict] = field(default_factory=list)
    target: float = 0.5  # utilization the outlook counts down to
    # The next 24 h on the current SKU if nothing new runs: background CU already charged.
    outlook: list[Window] = field(default_factory=list)
    # What made up the peak window, per item; cu_seconds is the share of that window only.
    peak_drivers: list[ItemRow] = field(default_factory=list)

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

    @property
    def at_risk(self) -> bool:
        """Whether this load crosses a throttling threshold on the capacity as sized now."""
        return any(
            self.current_breaches[key]
            for key in ("interactive_delay", "interactive_rejection", "background_rejection")
        )

    @property
    def peak_sku(self) -> str | None:
        """The SKU the peak was measured against, which a resize may since have changed."""
        peak = self.peak_window
        return self.capacity.sku_at(peak.start) if peak else self.capacity.sku

    @property
    def sku_changes(self) -> list[SkuChange]:
        """Resizes inside the reported range."""
        return [c for c in self.capacity.sku_changes if self.start <= c.at <= self.end]

    @property
    def recovery_at(self) -> datetime | None:
        """When committed load first drops below ``target`` if nothing new runs.

        Committed background CU only ever tails off, so the first window under the target
        stays under it. The outlook's first window means it is already below.
        """
        return next((w.start for w in self.outlook if w.utilization < self.target), None)

    @property
    def hold_off_seconds(self) -> float | None:
        """How long from the end of the range until ``recovery_at``."""
        recovery = self.recovery_at
        if recovery is None:
            return None
        return max((recovery - self.end).total_seconds(), 0.0)

    @property
    def headroom_cu_seconds(self) -> float | None:
        """Background CU-s that could be charged now without starting any throttle.

        Added background load raises each of the next 24 h of windows evenly, and
        committed load only tails off, so the 10-minute forward mean now is the binding
        horizon. Conservative: a job is charged when it ends, by which time committed
        load has fallen further.
        """
        if not self.outlook:
            return None
        now = self.outlook[0]
        spare = max(1.0 - now.interactive_delay, 0.0)
        return spare * now.budget_cu_seconds * self.rates.background_windows


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


def _budget(capacity: Capacity, lo: datetime, hi: datetime) -> float | None:
    """CU-seconds the capacity could deliver from ``lo`` to ``hi``, following resizes."""
    cuts = [lo, *(c.at for c in capacity.sku_changes if lo < c.at < hi), hi]
    total = 0.0
    for a, b in zip(cuts, cuts[1:], strict=False):
        cu = sku_cu(capacity.sku_at(a))
        if cu is None:
            return None
        total += cu * (b - a).total_seconds()
    return total


def _peak_drivers(operations: list[Operation], windows: list[Window], rates: Rates,
                  top: int = 5) -> list[ItemRow]:
    """Per-item CU-s in the peak window, largest first."""
    if not windows:
        return []
    peak = max(windows, key=lambda w: w.utilization)
    rows: dict[tuple, ItemRow] = {}
    for operation, cu in window_contributions(operations, peak.start, rates):
        key = (operation.workspace_name, operation.item_kind, operation.item_name)
        row = rows.setdefault(key, ItemRow(*key))
        row.cu_seconds += cu
        row.operations += 1
        if operation.exactness != "exact":
            row.exactness = "estimated"
    return sorted(rows.values(), key=lambda r: -r.cu_seconds)[:top]


def analyse(
    collection: Collection,
    rates: Rates,
    start: datetime,
    end: datetime,
    *,
    cost_rows: list[tuple[float, str, str]] | None = None,
    activity_events: list[dict] | None = None,
    target: float = 0.5,
) -> Analysis:
    operations = [op for op in collection.operations if op.end and start <= op.end <= end]
    capacity = collection.capacity
    # Background CU from runs that ended before `start` still spreads into the first
    # 24h of windows; build_timeline clips each spread to the range.
    smoothed = [op for op in collection.operations if op.end and op.end <= end]
    windows = (
        build_timeline(
            smoothed,
            start,
            end,
            sku_cu(capacity.sku_at(start)) or 0.0,
            rates,
            cu_changes=[(c.at, sku_cu(c.new) or 0.0) for c in capacity.sku_changes],
        )
        if capacity.base_cu
        else []
    )
    current = windows
    if capacity.base_cu and any(c.at > start for c in capacity.sku_changes):
        current = build_timeline(smoothed, start, end, capacity.base_cu, rates)
    horizon = timedelta(seconds=rates.background_windows * rates.window_seconds)
    outlook = (
        build_timeline(smoothed, end, end + horizon, capacity.base_cu, rates)
        if capacity.base_cu
        else []
    )

    days_map: dict[str, DayRow] = {}
    for operation in operations:
        key = operation.end.strftime("%Y-%m-%d")
        if key not in days_map:
            midnight = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=UTC)
            days_map[key] = DayRow(
                date=key, budget_cu_seconds=_budget(capacity, midnight, midnight + timedelta(days=1))
            )
        row = days_map[key]
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
        current_breaches=throttle_breaches(current),
        current_peak_utilization=max((w.utilization for w in current), default=None),
        total_cu_seconds=sum(op.cu_seconds for op in operations),
        total_operations=len(operations),
        users={op.user for op in operations if op.user},
        cost_rows=cost_rows,
        activity_events=activity_events or [],
        target=target,
        outlook=outlook,
        peak_drivers=_peak_drivers(smoothed, windows, rates),
    )
