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

    def __init__(self, sessions, items=None, runs=None, topologies=None):
        self._sessions = sessions
        self._items = items or []
        self._runs = runs or {}
        self._topologies = topologies or {}

    def default_pool(self, workspace_id):
        return POOL

    def livy_sessions(self, workspace_id):
        return self._sessions

    def items(self, workspace_id):
        return self._items

    def job_instances(self, workspace_id, item_id):
        return self._runs.get(item_id, [])

    def eventstream_topology(self, workspace_id, item_id):
        return self._topologies.get(item_id, {})


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


def test_dataflow_runs_are_priced_as_estimates(rates, capacity):
    items = [{"id": "d1", "type": "Dataflow", "displayName": "extract"}]
    runs = {"d1": [
        {"id": "r1", "status": "Completed", "invokeType": "Manual",
         "startTimeUtc": "2026-09-01T06:00:00.1234567", "endTimeUtc": "2026-09-01T06:05:00.1234567"},
        {"id": "r2", "status": "InProgress", "invokeType": "Manual",
         "startTimeUtc": "2026-09-01T07:00:00", "endTimeUtc": None},
        {"id": "r3", "status": "Completed", "invokeType": "Manual",
         "startTimeUtc": "2026-07-01T06:00:00", "endTimeUtc": "2026-07-01T06:05:00"},
    ]}
    result = collect(StubClient([], items, runs), capacity, rates, datetime(2026, 8, 1, tzinfo=UTC))
    [run] = [op for op in result.operations if op.source == "dataflow"]
    # 300 s inside the CI/CD first tier: 12 CU * 300 s.
    assert run.cu_seconds == pytest.approx(3600.0)
    assert run.exactness == "estimated"
    assert run.item_kind == "Dataflow"
    assert run.succeeded
    assert not any("Dataflow" in entry for entry in result.unaccounted)
    assert any("Dataflow Gen2 CU is estimated" in w for w in result.warnings)


def test_running_eventstream_is_listed_as_unaccounted(rates, capacity):
    items = [{"id": "e1", "type": "Eventstream", "displayName": "Monitoring_Eventstream"}]
    topologies = {"e1": {
        "streams": [{"name": "s", "status": "Running"}],
        "destinations": [{"name": "d", "status": "Running"}],
    }}
    result = collect(
        StubClient([], items, topologies=topologies), capacity, rates,
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    [entry] = [e for e in result.unaccounted if "Eventstream" in e and "Monitoring" in e]
    assert "analytics" in entry
    assert "running" in entry
    # 0.222 CU flat against an F4's 4 CU base.
    assert "flat 0.222 CU, ~6% of this F4" in entry
    assert not any("topology" in w for w in result.warnings)


def test_stopped_eventstream_is_not_listed(rates, capacity):
    items = [{"id": "e1", "type": "Eventstream", "displayName": "Monitoring_Eventstream"}]
    topologies = {"e1": {"streams": [{"name": "s", "status": "Paused"}]}}
    result = collect(
        StubClient([], items, topologies=topologies), capacity, rates,
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert not any("Monitoring_Eventstream" in entry for entry in result.unaccounted)


def test_unreadable_eventstream_topology_is_warned(rates, capacity):
    # A 403 or other FabricError comes back as {}; it must not look like a paused stream.
    items = [{"id": "e1", "type": "Eventstream", "displayName": "Monitoring_Eventstream"}]
    result = collect(
        StubClient([], items), capacity, rates, datetime(2026, 8, 1, tzinfo=UTC)
    )
    assert not any("Monitoring_Eventstream" in entry for entry in result.unaccounted)
    [warning] = [w for w in result.warnings if "topology" in w]
    assert "'Monitoring_Eventstream' (analytics)" in warning
