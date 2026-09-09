"""Collection, and above all the high-concurrency labelling contract."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fabric_capacity_monitor.collect import collect, duration_seconds
from fabric_capacity_monitor.model import Capacity
from fabric_capacity_monitor.report import analyse
from fabric_capacity_monitor.report import text as text_report

FIXTURES = Path(__file__).parent / "fixtures"
WS_ID = "00000000-0000-0000-0000-00000000000w"

POOL = {
    "name": "Starter Pool",
    "nodeSize": "Medium",
    "autoScale": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 2},
    "dynamicExecutorAllocation": {"enabled": True, "minExecutors": 1, "maxExecutors": 1},
}


class StubClient:
    """Stands in for FabricClient. Only the methods collect() uses are implemented."""

    def __init__(self, sessions, items=None):
        self._sessions = sessions
        self._items = items or []

    def default_pool(self, workspace_id):
        return POOL

    def livy_sessions(self, workspace_id):
        return self._sessions

    def items(self, workspace_id):
        return self._items

    def job_instances(self, workspace_id, item_id):
        return []


@pytest.fixture
def sessions():
    return json.loads((FIXTURES / "livy_sessions.json").read_text())


@pytest.fixture
def capacity():
    return Capacity(
        id="cap-1",
        name="test-capacity",
        sku="F4",
        region="westeurope",
        state="Active",
        workspaces=[{"id": WS_ID, "displayName": "analytics"}],
    )


def test_duration_units_are_honoured():
    assert duration_seconds({"value": 2, "timeUnit": "Minutes"}) == 120
    assert duration_seconds({"value": 1, "timeUnit": "Hours"}) == 3600
    assert duration_seconds({"value": 5, "timeUnit": "Seconds"}) == 5
    assert duration_seconds(None) == 0.0


def test_spark_sessions_become_exact_operations(rates, sessions, capacity):
    since = datetime(2026, 8, 1, tzinfo=UTC)
    result = collect(StubClient(sessions), capacity, rates, since)
    assert len(result.operations) == 2
    # 300 s on a 16-vCore pool == 8 CU * 300 s.
    ingest = next(op for op in result.operations if op.item_name == "ingest_customers")
    assert ingest.cu_seconds == pytest.approx(2400.0)
    assert ingest.exactness == "exact"
    assert ingest.succeeded


def test_failed_and_queued_state_is_preserved(rates, sessions, capacity):
    since = datetime(2026, 8, 1, tzinfo=UTC)
    result = collect(StubClient(sessions), capacity, rates, since)
    report = next(op for op in result.operations if op.item_name == "build_report")
    assert report.failed
    assert report.queued_seconds == 45
    assert report.user_type == "ServicePrincipal"


def test_sessions_ending_before_the_window_are_dropped(rates, sessions, capacity):
    since = datetime(2026, 9, 1, 2, 30, tzinfo=UTC)
    result = collect(StubClient(sessions), capacity, rates, since)
    assert [op.item_name for op in result.operations] == ["build_report"]


def test_high_concurrency_raises_a_warning(rates, sessions, capacity):
    since = datetime(2026, 8, 1, tzinfo=UTC)
    result = collect(StubClient(sessions), capacity, rates, since)
    assert result.high_concurrency_present
    assert any("per-SESSION, not per-notebook" in w for w in result.warnings)


def test_without_high_concurrency_there_is_no_such_warning(rates, sessions, capacity):
    plain = [dict(s, isHighConcurrency=False) for s in sessions]
    result = collect(StubClient(plain), capacity, rates, datetime(2026, 8, 1, tzinfo=UTC))
    assert not result.high_concurrency_present
    assert not any("per-notebook" in w for w in result.warnings)


def test_report_labels_the_breakdown_as_sessions_under_high_concurrency(rates, sessions, capacity):
    """The contract from the plan: never claim per-notebook CU when HC is in play."""
    since = datetime(2026, 8, 1, tzinfo=UTC)
    result = collect(StubClient(sessions), capacity, rates, since)
    analysis = analyse(
        result, rates, since, datetime(2026, 9, 2, tzinfo=UTC)
    )
    rendered = text_report.render(analysis, by_item=True)
    assert "BY SPARK SESSION" in rendered
    assert "BY ITEM" not in rendered
    assert "opened the session" in rendered.lower() or "OPENED" in rendered


def test_report_says_by_item_when_high_concurrency_is_off(rates, sessions, capacity):
    plain = [dict(s, isHighConcurrency=False) for s in sessions]
    since = datetime(2026, 8, 1, tzinfo=UTC)
    result = collect(StubClient(plain), capacity, rates, since)
    analysis = analyse(result, rates, since, datetime(2026, 9, 2, tzinfo=UTC))
    rendered = text_report.render(analysis, by_item=True)
    assert "BY ITEM" in rendered
    assert "BY SPARK SESSION" not in rendered


def test_dataflows_are_reported_as_unaccounted(rates, sessions, capacity):
    items = [{"id": "d1", "type": "Dataflow", "displayName": "extract"}]
    result = collect(
        StubClient(sessions, items), capacity, rates, datetime(2026, 8, 1, tzinfo=UTC)
    )
    assert any("Dataflow Gen2" in entry for entry in result.unaccounted)
