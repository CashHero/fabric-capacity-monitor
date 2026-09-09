"""The smoothing model: 24-hour background spread, forward-window throttling, carryforward."""

from datetime import UTC, datetime, timedelta

import pytest

from fabric_capacity_monitor.model import BACKGROUND, EXACT, INTERACTIVE, Operation
from fabric_capacity_monitor.smoothing import build_timeline, floor_window, throttle_breaches

START = datetime(2026, 9, 1, tzinfo=UTC)


def make_op(end, cu_seconds, kind=BACKGROUND):
    return Operation(
        source="spark",
        exactness=EXACT,
        workspace_id="w",
        workspace_name="w",
        item_id="i",
        item_name="nb",
        item_kind="Notebook",
        operation_name="Notebook run",
        status="Succeeded",
        start=end - timedelta(seconds=60),
        end=end,
        duration_seconds=60.0,
        cu_seconds=cu_seconds,
        utilization_type=kind,
    )


def test_floor_window_snaps_to_the_30_second_grid():
    moment = datetime(2026, 9, 1, 12, 0, 47, tzinfo=UTC)
    assert floor_window(moment, 30) == datetime(2026, 9, 1, 12, 0, 30, tzinfo=UTC)


def test_background_cu_is_conserved_when_the_range_covers_the_whole_spread(rates):
    # One day of load, but three days of window so the full 24h spread lands inside.
    op = make_op(START + timedelta(hours=1), 100_000.0)
    windows = build_timeline([op], START, START + timedelta(days=3), base_cu=4.0, rates=rates)
    total = sum(w.cu_seconds for w in windows)
    assert total == pytest.approx(100_000.0, rel=1e-9)


def test_background_cu_is_spread_evenly_not_dumped_in_one_window(rates):
    op = make_op(START + timedelta(hours=1), 28_800.0)  # 2880 windows -> 10 CU-s each
    windows = build_timeline([op], START, START + timedelta(days=2), base_cu=4.0, rates=rates)
    loaded = [w for w in windows if w.cu_seconds > 0]
    assert len(loaded) == rates.background_windows
    assert all(w.cu_seconds == pytest.approx(10.0) for w in loaded)


def test_interactive_cu_lands_entirely_in_the_completion_window(rates):
    op = make_op(START + timedelta(hours=1), 500.0, kind=INTERACTIVE)
    windows = build_timeline([op], START, START + timedelta(hours=2), base_cu=4.0, rates=rates)
    loaded = [w for w in windows if w.cu_seconds > 0]
    assert len(loaded) == 1
    assert loaded[0].interactive_cu_seconds == pytest.approx(500.0)


def test_utilization_is_relative_to_the_window_budget(rates):
    # An F4 window budget is 4 CU * 30 s = 120 CU-s. Spread 120 * 2880 to fill every window.
    op = make_op(START, 120.0 * rates.background_windows)
    windows = build_timeline([op], START, START + timedelta(days=1), base_cu=4.0, rates=rates)
    assert windows[0].utilization == pytest.approx(1.0)


def test_over_budget_load_adds_carryforward_and_then_burns_it_down(rates):
    # Twice the budget for the whole spread, so every covered window is over.
    op = make_op(START, 240.0 * rates.background_windows)
    windows = build_timeline([op], START, START + timedelta(days=2), base_cu=4.0, rates=rates)
    assert windows[0].carry_add > 0
    peak_carry = max(w.carry_cumulative for w in windows)
    assert peak_carry > 0
    # Once the spread ends, idle windows burn the balance back down to zero.
    assert windows[-1].carry_cumulative == pytest.approx(0.0)
    assert sum(w.carry_burndown for w in windows) == pytest.approx(
        sum(w.carry_add for w in windows)
    )


def test_throttle_horizons_are_forward_looking(rates):
    op = make_op(START + timedelta(hours=12), 240.0 * rates.background_windows)
    windows = build_timeline([op], START, START + timedelta(days=3), base_cu=4.0, rates=rates)
    # The window right before the load starts already sees it in its 24h forward mean,
    # but not in its own utilization.
    index = next(i for i, w in enumerate(windows) if w.cu_seconds > 0)
    just_before = windows[index - 1]
    assert just_before.cu_seconds == 0
    assert just_before.background_reject > 0


def test_breaches_are_counted_per_horizon(rates):
    op = make_op(START, 500.0 * rates.background_windows)
    windows = build_timeline([op], START, START + timedelta(days=2), base_cu=4.0, rates=rates)
    breaches = throttle_breaches(windows)
    assert breaches["over_capacity"] > 0
    assert breaches["background_rejection"] > 0


def test_quiet_capacity_reports_no_breaches(rates):
    op = make_op(START, 1.0)
    windows = build_timeline([op], START, START + timedelta(days=1), base_cu=4.0, rates=rates)
    assert throttle_breaches(windows) == {
        "interactive_delay": 0,
        "interactive_rejection": 0,
        "background_rejection": 0,
        "over_capacity": 0,
    }


def test_empty_input_produces_windows_without_crashing(rates):
    windows = build_timeline([], START, START + timedelta(hours=1), base_cu=4.0, rates=rates)
    assert len(windows) == 121
    assert all(w.cu_seconds == 0 for w in windows)


def test_zero_length_range_is_handled(rates):
    windows = build_timeline([], START, START, base_cu=4.0, rates=rates)
    assert len(windows) == 1


def test_operations_without_an_end_time_are_ignored(rates):
    op = make_op(START, 100.0)
    op.end = None
    assert build_timeline([op], START, START + timedelta(hours=1), 4.0, rates)[0].cu_seconds == 0
