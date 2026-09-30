"""Renderers: they must run, and must not overstate what they know."""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from fabric_capacity_monitor.collect import Collection
from fabric_capacity_monitor.model import (
    BACKGROUND,
    ESTIMATED,
    EXACT,
    Capacity,
    Operation,
    SkuChange,
)
from fabric_capacity_monitor.report import analyse, json_out
from fabric_capacity_monitor.report import html as html_report
from fabric_capacity_monitor.report import text as text_report

START = datetime(2026, 9, 1, tzinfo=UTC)
END = START + timedelta(days=2)


def _op(name, cu, when, exactness=EXACT, status="Succeeded"):
    return Operation(
        source="spark",
        exactness=exactness,
        workspace_id="w",
        workspace_name="analytics",
        item_id="i",
        item_name=name,
        item_kind="Notebook",
        operation_name="Notebook run",
        status=status,
        start=when - timedelta(minutes=5),
        end=when,
        duration_seconds=300.0,
        cu_seconds=cu,
        utilization_type=BACKGROUND,
        user="someone@example.com",
    )


@pytest.fixture
def analysis(rates):
    capacity = Capacity(id="c", name="demo", sku="F4", region="westeurope", state="Active",
                        workspaces=[{"id": "w", "displayName": "analytics"}])
    collection = Collection(capacity=capacity)
    collection.operations = [
        _op("ingest", 2400.0, START + timedelta(hours=2)),
        _op("transform", 9600.0, START + timedelta(hours=3)),
        _op("broken", 100.0, START + timedelta(hours=4), status="Failed"),
        _op("pipeline", 20.16, START + timedelta(hours=5), exactness=ESTIMATED),
    ]
    collection.unaccounted = ["OneLake transactions"]
    return analyse(collection, rates, START, END)


def test_text_report_renders_headline_numbers(analysis):
    out = text_report.render(analysis, by_item=True, by_workspace=True)
    assert "demo" in out
    assert "F4" in out
    assert "SUMMARY:" in out
    assert "transform" in out
    assert "NOT COUNTED IN THE TOTALS ABOVE" in out


def test_text_report_marks_estimates(analysis):
    out = text_report.render(analysis, by_item=True)
    assert "~ = estimated" in out
    pipeline_line = next(line for line in out.splitlines() if "pipeline" in line)
    assert "~" in pipeline_line


def test_quiet_capacity_summarises_as_healthy(analysis):
    assert "healthy" in text_report.render(analysis)


def test_json_round_trips(analysis):
    payload = json.loads(json_out.render(analysis))
    assert payload["capacity"]["sku"] == "F4"
    assert payload["summary"]["total_operations"] == 4
    assert len(payload["operations"]) == 4
    assert payload["unaccounted"]


def test_html_is_self_contained(analysis):
    page = html_report.render(analysis)
    # Opened from disk there is no Content-Type header, so without this browsers
    # fall back to windows-1252 and every "·" renders as "Â·".
    assert page.startswith('<meta charset="utf-8">')
    assert "<title>Capacity · demo</title>" in page
    assert "<svg" in page
    # No external resources of any kind: the page must render offline.
    for marker in ("http://", "https://", "cdn.", "<script"):
        assert marker not in page


def test_html_escapes_item_names(rates):
    capacity = Capacity(id="c", name="demo", sku="F4",
                        workspaces=[{"id": "w", "displayName": "analytics"}])
    collection = Collection(capacity=capacity)
    collection.operations = [_op("<img src=x onerror=alert(1)>", 10.0, START + timedelta(hours=1))]
    page = html_report.render(analyse(collection, rates, START, END))
    assert "<img src=x" not in page
    assert "&lt;img" in page


def test_performance_delta_compares_against_the_prior_week(rates):
    capacity = Capacity(id="c", name="demo", sku="F4",
                        workspaces=[{"id": "w", "displayName": "analytics"}])
    collection = Collection(capacity=capacity)
    collection.operations = [
        _op("job", 200.0, START + timedelta(hours=1)),
        _op("job", 100.0, START - timedelta(days=6)),  # inside the prior-week baseline
    ]
    analysis = analyse(collection, rates, START, END)
    row = next(r for r in analysis.items if r.item_name == "job")
    assert row.performance_delta == pytest.approx(100.0)


def test_average_utilization_ignores_idle_windows(analysis):
    # An idle overnight must not make the capacity look healthier than it is.
    assert analysis.average_utilization is not None
    assert analysis.average_utilization > 0


def test_timeline_includes_carry_in_from_before_the_range(rates):
    # A run that ended 12h before the range is still being smoothed into its first
    # 12h of windows, but it is not part of the range's totals.
    capacity = Capacity(id="c", name="demo", sku="F4",
                        workspaces=[{"id": "w", "displayName": "analytics"}])
    collection = Collection(capacity=capacity)
    collection.operations = [_op("overnight", 2880.0, START - timedelta(hours=12))]
    analysis = analyse(collection, rates, START, END)
    assert analysis.windows[0].background_cu_seconds == pytest.approx(1.0)
    assert analysis.windows[1439].background_cu_seconds == pytest.approx(1.0)
    assert analysis.windows[1440].background_cu_seconds == pytest.approx(0.0)
    assert analysis.total_cu_seconds == 0
    assert analysis.days == []


def _resized_capacity():
    # Was F4 until noon on day one, F8 since.
    return Capacity(
        id="c", name="demo", sku="F8", workspaces=[{"id": "w", "displayName": "analytics"}],
        sku_changes=[SkuChange(at=START + timedelta(hours=12), previous="F4", new="F8")],
    )


def test_sku_at_follows_resizes():
    capacity = _resized_capacity()
    assert capacity.sku_at(START) == "F4"
    assert capacity.sku_at(START + timedelta(hours=12)) == "F8"
    assert capacity.sku_at(END) == "F8"


def test_a_resize_is_not_applied_to_the_time_before_it(rates):
    collection = Collection(capacity=_resized_capacity())
    collection.operations = [_op("ingest", 100_000.0, START + timedelta(hours=1))]
    analysis = analyse(collection, rates, START, END)

    before = next(w for w in analysis.windows if w.start == START + timedelta(hours=6))
    after = next(w for w in analysis.windows if w.start == START + timedelta(hours=18))
    # The same smoothed CU is twice the share of an F4 as of an F8.
    assert before.cu_seconds == pytest.approx(after.cu_seconds)
    assert before.utilization == pytest.approx(2 * after.utilization)
    assert analysis.peak_sku == "F4"

    # Day one had 12 h of F4 and 12 h of F8.
    assert analysis.days[0].budget_cu_seconds == pytest.approx((4 + 8) * 43_200)

    out = text_report.render(analysis)
    assert "Resized               F4 → F8 at 2026-09-01 12:00 UTC" in out
    assert "of F4 before resize" in out.splitlines()[-2]
    assert "Current utilization" in out
    # The resize is a quarter of the way through a 2-day range: sparkline bucket 15 of 60.
    marker = next(line for line in out.splitlines() if "^" in line)
    assert marker.strip() == "^ F4→F8"
    assert marker.index("^") - 4 == 15
    assert "F4 → F8" in html_report.render(analysis)


def test_verdict_judges_the_load_against_the_current_sku(rates):
    # 1.5x an F4 all day: throttled while it was an F4, comfortable on today's F8.
    collection = Collection(capacity=_resized_capacity())
    collection.operations = [_op("ingest", 180.0 * rates.background_windows, START)]
    analysis = analyse(collection, rates, START, END)

    assert analysis.breaches["background_rejection"] > 0  # history is kept
    assert not analysis.at_risk
    assert analysis.current_peak_utilization == pytest.approx(0.75)
    summary = text_report.render(analysis).splitlines()[-2]
    assert summary.startswith("SUMMARY: healthy on F8 · peak 75.0% of F8")
    assert json.loads(json_out.render(analysis))["summary"]["at_risk"] is False


def test_verdict_still_flags_a_load_the_current_sku_cannot_carry(rates):
    collection = Collection(capacity=_resized_capacity())
    collection.operations = [_op("ingest", 400.0 * rates.background_windows, START)]
    analysis = analyse(collection, rates, START, END)
    assert analysis.at_risk
    assert "THROTTLING RISK on F8" in text_report.render(analysis)


def test_a_resize_near_the_end_puts_its_label_left_of_the_marker(rates):
    capacity = _resized_capacity()
    capacity.sku_changes[0].at = END - timedelta(minutes=30)
    collection = Collection(capacity=capacity)
    collection.operations = [_op("ingest", 1000.0, START)]
    out = text_report.render(analyse(collection, rates, START, END))
    marker = next(line for line in out.splitlines() if "^" in line)
    assert marker.rstrip().endswith("F4→F8 ^")
    assert marker.index("^") - 4 == 59


def _loaded_until_end(rates):
    # On an F4: a run filling the capacity whose spread ends 4 h after END, plus one
    # filling 40% for the rest of the day.
    capacity = Capacity(id="c", name="demo", sku="F4",
                        workspaces=[{"id": "w", "displayName": "analytics"}])
    collection = Collection(capacity=capacity)
    collection.operations = [
        _op("nightly", 120.0 * rates.background_windows, END - timedelta(hours=20)),
        _op("hourly", 48.0 * rates.background_windows, END - timedelta(hours=2)),
    ]
    return analyse(collection, rates, START, END)


def test_outlook_says_when_committed_load_drops_below_the_target(rates):
    analysis = _loaded_until_end(rates)
    assert analysis.outlook[0].utilization == pytest.approx(1.4)
    assert analysis.recovery_at == END + timedelta(hours=4)
    assert analysis.headroom_cu_seconds == 0

    out = text_report.render(analysis)
    assert "Below 50% target      in 4h 00m (2026-09-03 04:00 UTC)" in out
    payload = json.loads(json_out.render(analysis))
    assert payload["outlook"]["recovery_at"] == "2026-09-03T04:00:00+00:00"
    assert payload["outlook"]["hold_off_seconds"] == 4 * 3600
    assert "Outlook" in html_report.render(analysis)


def test_outlook_honours_a_higher_target(rates):
    collection = _loaded_until_end(rates).collection
    analysis = analyse(collection, rates, START, END, target=1.5)
    assert analysis.recovery_at == END


def test_idle_capacity_is_already_below_target_with_full_headroom(analysis, rates):
    assert analysis.recovery_at == END
    assert analysis.headroom_cu_seconds == pytest.approx(120.0 * rates.background_windows)
    assert "already below" in text_report.render(analysis)


def _f4(rates, operations):
    capacity = Capacity(id="c", name="demo", sku="F4",
                        workspaces=[{"id": "w", "displayName": "analytics"}])
    collection = Collection(capacity=capacity)
    collection.operations = operations
    return analyse(collection, rates, START, END)


def test_outlook_drivers_add_up_to_the_committed_load_and_keep_estimates_marked(rates):
    analysis = _f4(rates, [
        _op("nightly", 96.0 * rates.background_windows, END - timedelta(hours=20)),
        _op("pipeline", 12.0 * rates.background_windows, END - timedelta(hours=1),
            exactness=ESTIMATED),
        _op("old", 999_999.0, START),  # spread finished before the outlook starts
    ])
    assert [row.item_name for row in analysis.outlook_drivers] == ["nightly", "pipeline"]
    payload = json.loads(json_out.render(analysis))
    shares = [row["utilization"] for row in payload["outlook_drivers"]]
    assert shares == pytest.approx([0.8, 0.1])
    assert sum(shares) == pytest.approx(payload["outlook"]["utilization_now"])

    out = text_report.render(analysis)
    assert "Committed load now, 90.0% of F4, made up of" in out
    driver_line = next(line for line in out.splitlines() if "pipeline" in line)
    assert driver_line.split()[:2] == ["pipeline", "~"]
    assert "~ = estimated, not measured" in out  # legend without --by-item


def test_calm_capacity_has_no_drivers_section(rates):
    analysis = _f4(rates, [_op("old", 999_999.0, START)])  # spread finished before now
    assert analysis.outlook[0].utilization == 0
    assert "holding utilization up" not in html_report.render(analysis)


def test_headroom_is_the_spare_share_of_the_next_24_hours(rates):
    # 40% of an F4 committed now: 60% of every window in the next 24 h is spare.
    analysis = _f4(rates, [_op("hourly", 48.0 * rates.background_windows,
                                END - timedelta(hours=2))])
    assert analysis.headroom_cu_seconds == pytest.approx(0.6 * 120.0 * rates.background_windows)


def test_outlook_drivers_show_five_and_sum_the_rest(rates):
    analysis = _f4(rates, [
        _op(f"job{i}", (10.0 + i) * rates.background_windows, END - timedelta(hours=1))
        for i in range(7)
    ])
    assert len(analysis.outlook_drivers) == 7
    assert len(json.loads(json_out.render(analysis))["outlook_drivers"]) == 7

    out = text_report.render(analysis)
    lines = out.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("  Committed load now"))
    rows = lines[first + 1 : first + 7]
    assert [row.split()[0] for row in rows[:5]] == ["job6", "job5", "job4", "job3", "job2"]
    assert rows[5].split()[:3] == ["2", "more", "items"]
    points = [float(row.split()[-2]) for row in rows]
    assert sum(points) == pytest.approx(analysis.outlook[0].utilization * 100, abs=0.1)
    assert "2 more items" in html_report.render(analysis)


def test_outlook_drivers_say_session_opener_under_high_concurrency(rates):
    analysis = _f4(rates, [
        replace(_op("opener", 48.0 * rates.background_windows, END - timedelta(hours=1)),
                is_high_concurrency=True),
    ])
    out = text_report.render(analysis)
    lines = out.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("  Committed load now"))
    assert "attributed to the notebook that OPENED it" in lines[first + 1]
    page = html_report.render(analysis)
    drivers = page[page.index("holding utilization up"):]
    assert "attributed to the notebook that opened it" in drivers


def test_a_long_estimated_name_keeps_its_mark(rates):
    name = "a_very_long_dataflow_name_that_goes_on_and_on"
    analysis = _f4(rates, [
        _op(name, 12.0 * rates.background_windows, END - timedelta(hours=1),
            exactness=ESTIMATED),
    ])
    out = text_report.render(analysis, by_item=True)
    marked = [line for line in out.splitlines() if name[:30] in line]
    assert len(marked) == 2  # the outlook drivers and the by-item table
    for line in marked:
        assert " ~ " in line


def test_outlook_chart_shades_only_above_100_percent():
    chart = html_report._area_chart([0.8] * 10, target=0.5)
    assert 'class="over"' not in chart
    assert 'class="target"' in chart
    assert 'class="limit"' in chart
    assert 'class="over"' in html_report._area_chart([1.2] * 10, target=0.5)


def test_outlook_that_never_reaches_the_target_still_renders(rates):
    analysis = _f4(rates, [_op("flat", 96.0 * rates.background_windows,
                               END - timedelta(hours=1))])
    analysis = replace(analysis, outlook=analysis.outlook[:100])
    assert analysis.recovery_at is None
    assert analysis.hold_off_seconds is None
    assert "still above the target after 24 h" in text_report.render(analysis)
    assert "not within 24 h" in html_report.render(analysis)
    assert json.loads(json_out.render(analysis))["outlook"]["recovery_at"] is None


def _x_labels(svg):
    return re.findall(r'class="xlab">([^<]+)<', svg)


def test_charts_label_the_time_axis_in_utc(analysis):
    day = html_report._area_chart([0.5] * 2880, span=(START, START + timedelta(hours=24)))
    assert 1 < len(_x_labels(day)) <= 8
    assert _x_labels(day)[0] == "Sep 01"  # midnight gets the date, not "00:00"
    assert "06:00" in _x_labels(day)

    fortnight = html_report._area_chart(
        [0.5] * 40320, span=(START, START + timedelta(days=14))
    )
    assert 1 < len(_x_labels(fortnight)) <= 8
    assert all(re.fullmatch(r"Sep \d\d", label) for label in _x_labels(fortnight))

    assert 'class="xlab"' not in html_report._area_chart([0.5] * 10)
    assert 'class="xlab"' in html_report.render(analysis)
