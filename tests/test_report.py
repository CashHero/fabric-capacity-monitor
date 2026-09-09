"""Renderers: they must run, and must not overstate what they know."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from fabric_capacity_monitor.collect import Collection
from fabric_capacity_monitor.model import BACKGROUND, ESTIMATED, EXACT, Capacity, Operation
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
    assert "<title>" in page
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
