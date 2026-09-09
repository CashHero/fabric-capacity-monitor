"""Reconstruct Fabric's smoothed capacity timeline from discrete operations.

Fabric does not charge an operation against the instant it ran. Background operations
are spread evenly across the following 24 hours, and throttling is decided by the mean
utilization of a *forward* window, not by any single instant. Reproducing that is what
turns a list of job durations into the utilization and throttling curves the Capacity
Metrics app shows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .model import BACKGROUND, Operation, Window
from .rates import Rates


def floor_window(moment: datetime, window_seconds: int) -> datetime:
    """Round ``moment`` down to the start of its smoothing window."""
    epoch = moment.timestamp()
    return datetime.fromtimestamp(
        epoch - (epoch % window_seconds), tz=UTC
    )


def _forward_means(values: list[float], span: int) -> list[float]:
    """Mean of each forward-looking window of ``span`` entries, via a prefix sum.

    The tail averages over however many windows remain, which keeps the final entries
    meaningful rather than artificially decaying toward zero.
    """
    count = len(values)
    if count == 0:
        return []
    prefix = [0.0] * (count + 1)
    for index, value in enumerate(values):
        prefix[index + 1] = prefix[index] + value
    out = [0.0] * count
    for index in range(count):
        stop = min(index + span, count)
        width = stop - index
        out[index] = (prefix[stop] - prefix[index]) / width if width else 0.0
    return out


def build_timeline(
    operations: list[Operation],
    start: datetime,
    end: datetime,
    base_cu: float,
    rates: Rates,
) -> list[Window]:
    """Spread ``operations`` across smoothing windows spanning ``start`` to ``end``.

    Background CU is distributed evenly over the 24 hours after an operation ends;
    interactive CU lands wholly in the window the operation completed in. The spread
    uses a difference array so cost stays linear in the number of windows rather than
    windows-times-operations.
    """
    window_seconds = rates.window_seconds
    origin = floor_window(start, window_seconds)
    last = floor_window(end, window_seconds)
    count = int((last - origin).total_seconds() // window_seconds) + 1
    if count <= 0:
        return []

    budget = base_cu * window_seconds  # CU-seconds available per window
    background_delta = [0.0] * (count + 1)
    interactive = [0.0] * count

    for operation in operations:
        if operation.cu_seconds <= 0 or operation.end is None:
            continue
        finish = floor_window(operation.end, window_seconds)
        first_index = int((finish - origin).total_seconds() // window_seconds)

        if operation.utilization_type != BACKGROUND:
            if 0 <= first_index < count:
                interactive[first_index] += operation.cu_seconds
            continue

        span = rates.background_windows
        per_window = operation.cu_seconds / span
        lo = first_index
        hi = first_index + span
        # Clip to the reporting range; work that spread outside it is genuinely not
        # part of these windows' utilization.
        lo_clipped = max(lo, 0)
        hi_clipped = min(hi, count)
        if hi_clipped <= lo_clipped:
            continue
        background_delta[lo_clipped] += per_window
        background_delta[hi_clipped] -= per_window

    background = [0.0] * count
    running = 0.0
    for index in range(count):
        running += background_delta[index]
        background[index] = running

    totals = [background[i] + interactive[i] for i in range(count)]
    utilization = [(totals[i] / budget) if budget else 0.0 for i in range(count)]

    delay = _forward_means(utilization, rates.interactive_delay_windows)
    reject = _forward_means(utilization, rates.interactive_reject_windows)
    background_reject = _forward_means(utilization, rates.background_windows)

    windows: list[Window] = []
    carry = 0.0
    for index in range(count):
        used = totals[index]
        add = max(used - budget, 0.0)
        burndown = 0.0
        if add:
            carry += add
        else:
            burndown = min(budget - used, carry)
            carry -= burndown
        windows.append(
            Window(
                start=origin + timedelta(seconds=index * window_seconds),
                cu_seconds=used,
                background_cu_seconds=background[index],
                interactive_cu_seconds=interactive[index],
                utilization=utilization[index],
                carry_add=add,
                carry_burndown=burndown,
                carry_cumulative=carry,
                interactive_delay=delay[index],
                interactive_reject=reject[index],
                background_reject=background_reject[index],
            )
        )
    return windows


def throttle_breaches(windows: list[Window]) -> dict[str, int]:
    """Count windows whose forward-window mean crossed each throttling threshold."""
    return {
        "interactive_delay": sum(1 for w in windows if w.interactive_delay > 1.0),
        "interactive_rejection": sum(1 for w in windows if w.interactive_reject > 1.0),
        "background_rejection": sum(1 for w in windows if w.background_reject > 1.0),
        "over_capacity": sum(1 for w in windows if w.utilization > 1.0),
    }
