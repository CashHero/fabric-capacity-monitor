"""Redistributing a shared session's CU across the runs inside it."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from fabric_capacity_monitor.model import BACKGROUND, EXACT, Operation
from fabric_capacity_monitor.subruns import apply_subruns

WHEN = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass
class Run:
    name: str
    duration_seconds: float


class Resolver:
    def __init__(self, mapping):
        self.mapping = mapping

    def resolve(self, session_id):
        return self.mapping.get(session_id, [])


class Broken:
    def resolve(self, session_id):
        raise RuntimeError("log query failed")


def session(cu, hc=True, session_id="s1"):
    return Operation(
        source="spark",
        exactness=EXACT,
        workspace_id="w",
        workspace_name="w",
        item_id="i",
        item_name="opener",
        item_kind="Notebook",
        operation_name="Notebook HC Pipeline Run",
        status="Succeeded",
        start=WHEN,
        end=WHEN + timedelta(seconds=100),
        duration_seconds=100.0,
        cu_seconds=cu,
        utilization_type=BACKGROUND,
        is_high_concurrency=hc,
        session_id=session_id,
    )


def test_cu_is_split_in_proportion_to_duration():
    resolver = Resolver({"s1": [Run("a", 75.0), Run("b", 25.0)]})
    out, split = apply_subruns([session(1000.0)], resolver)
    assert split == 1
    assert sorted((o.item_name, o.cu_seconds) for o in out) == [("a", 750.0), ("b", 250.0)]


def test_the_capacity_total_is_preserved():
    resolver = Resolver({"s1": [Run("a", 1.0), Run("b", 2.0), Run("c", 7.0)]})
    out, _ = apply_subruns([session(3333.0)], resolver)
    assert sum(o.cu_seconds for o in out) == pytest.approx(3333.0)


def test_non_high_concurrency_sessions_are_untouched():
    resolver = Resolver({"s1": [Run("a", 10.0)]})
    out, split = apply_subruns([session(500.0, hc=False)], resolver)
    assert split == 0
    assert out[0].item_name == "opener"


def test_unknown_sessions_are_left_alone_rather_than_guessed():
    out, split = apply_subruns([session(500.0)], Resolver({}))
    assert split == 0
    assert out[0].item_name == "opener"
    assert out[0].cu_seconds == 500.0


def test_zero_duration_subruns_do_not_divide_by_zero():
    resolver = Resolver({"s1": [Run("a", 0.0), Run("b", 0.0)]})
    out, split = apply_subruns([session(500.0)], resolver)
    assert split == 0
    assert out[0].cu_seconds == 500.0


def test_a_failing_resolver_never_loses_the_session():
    out, split = apply_subruns([session(500.0)], Broken())
    assert split == 0
    assert out[0].cu_seconds == 500.0


def test_split_operations_are_annotated():
    resolver = Resolver({"s1": [Run("a", 1.0)]})
    out, _ = apply_subruns([session(10.0)], resolver)
    assert "apportioned" in out[0].note
