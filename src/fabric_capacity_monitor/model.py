"""Normalised records shared by every data source and every renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

EXACT = "exact"
ESTIMATED = "estimated"

BACKGROUND = "background"
INTERACTIVE = "interactive"


def parse_fabric_time(value: str | None) -> datetime | None:
    """Parse a Fabric timestamp, which may or may not carry a ``Z`` suffix.

    Fabric is inconsistent: ``livySessions`` returns ``2026-09-09T02:50:42Z`` while
    ``jobs/instances`` returns ``2026-09-09T02:00:09.1017211`` with no zone at all.
    Naive values are treated as UTC, which is what Fabric means by them.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python's parser accepts at most 6 fractional digits; Fabric emits 7.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                break
        rest = tail[len(digits) :]
        text = f"{head}.{digits[:6]}{rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass
class Operation:
    """One capacity-consuming operation, normalised across sources."""

    source: str  # "spark" | "pipeline" | "dataflow"
    exactness: str  # EXACT | ESTIMATED
    workspace_id: str
    workspace_name: str
    item_id: str | None
    item_name: str
    item_kind: str
    operation_name: str
    status: str
    start: datetime | None
    end: datetime | None
    duration_seconds: float
    cu_seconds: float
    utilization_type: str = BACKGROUND
    queued_seconds: float = 0.0
    user: str | None = None
    user_type: str | None = None
    is_high_concurrency: bool = False
    session_id: str | None = None
    job_instance_id: str | None = None
    note: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status.lower() in {"succeeded", "completed", "success"}

    @property
    def failed(self) -> bool:
        return self.status.lower() in {"failed", "error"}

    @property
    def cancelled(self) -> bool:
        return self.status.lower() in {"cancelled", "canceled", "stopped"}


def sku_cu(sku: str | None) -> float | None:
    """Base capacity units implied by a SKU name, e.g. ``F4`` -> 4.0."""
    if not sku:
        return None
    digits = "".join(c for c in sku if c.isdigit())
    return float(digits) if digits else None


@dataclass
class SkuChange:
    """One resize of a capacity, as recorded by Azure Resource Graph."""

    at: datetime
    previous: str
    new: str


@dataclass
class Capacity:
    """A Fabric capacity and the workspaces assigned to it."""

    id: str
    name: str
    sku: str | None = None  # current SKU
    region: str | None = None
    state: str | None = None
    resource_id: str | None = None
    workspaces: list[dict] = field(default_factory=list)
    sku_changes: list[SkuChange] = field(default_factory=list)  # oldest first

    @property
    def base_cu(self) -> float | None:
        """Base capacity units implied by the current SKU, e.g. ``F4`` -> 4.0."""
        return sku_cu(self.sku)

    def sku_at(self, moment: datetime) -> str | None:
        """The SKU in effect at ``moment``: what the first later resize changed *from*."""
        for change in self.sku_changes:
            if change.at > moment:
                return change.previous
        return self.sku

    def daily_budget_cu_seconds(self) -> float | None:
        base = self.base_cu
        return base * 86400.0 if base is not None else None


@dataclass
class Window:
    """One smoothing window of the reconstructed capacity timeline."""

    start: datetime
    cu_seconds: float
    background_cu_seconds: float
    interactive_cu_seconds: float
    utilization: float  # fraction of the window's CU budget, 1.0 == 100%
    carry_add: float = 0.0
    carry_burndown: float = 0.0
    carry_cumulative: float = 0.0
    interactive_delay: float = 0.0  # forward 10-minute mean utilization
    interactive_reject: float = 0.0  # forward 60-minute mean utilization
    background_reject: float = 0.0  # forward 24-hour mean utilization
    budget_cu_seconds: float = 0.0  # what the SKU in effect could deliver in this window

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=30)
