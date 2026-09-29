"""Optional user configuration."""

import pytest

from fabric_capacity_monitor.config import resolve_target


def test_target_prefers_the_flag_then_the_config_then_50_percent():
    assert resolve_target({"target_utilization": 70}, 80) == pytest.approx(0.8)
    assert resolve_target({"target_utilization": 70}, None) == pytest.approx(0.7)
    assert resolve_target({}, None) == pytest.approx(0.5)


@pytest.mark.parametrize("bad", [0, -5, 101])
def test_target_outside_0_to_100_is_rejected(bad):
    with pytest.raises(ValueError):
        resolve_target({}, bad)
