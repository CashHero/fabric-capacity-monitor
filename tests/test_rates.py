"""Rates load from data, and every one of them can be overridden."""

import pytest

from fabric_capacity_monitor.rates import Rates


def test_packaged_rates_load(rates):
    assert rates.vcores_per_cu == 2.0
    assert rates.window_seconds == 30
    assert rates.background_windows == 2880
    assert rates.as_of


def test_node_sizes_are_known(rates):
    assert rates.node_vcores("Small") == 4
    assert rates.node_vcores("Medium") == 8
    assert rates.node_vcores("XXLarge") == 64
    assert rates.node_vcores("nonsense") is None
    assert rates.node_vcores(None) is None


def test_overrides_merge_without_dropping_neighbours():
    custom = Rates.load({"spark": {"vcores_per_cu": 4.0}})
    assert custom.vcores_per_cu == 4.0
    # The sibling table survived the merge.
    assert custom.node_vcores("Medium") == 8


def test_nested_overrides_merge(rates):
    custom = Rates.load({"spark": {"node_vcores": {"Medium": 99}}})
    assert custom.node_vcores("Medium") == 99
    assert custom.node_vcores("Small") == 4


def test_orchestration_rate_converts_hours_to_seconds(rates):
    assert rates.orchestration_cu_seconds_per_activity == pytest.approx(20.16)
