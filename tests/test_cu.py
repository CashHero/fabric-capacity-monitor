"""CU arithmetic. The numbers here are taken from Microsoft's published rates."""

import pytest

from fabric_capacity_monitor.cu import (
    dataflow_cu_seconds,
    orchestration_cu_seconds,
    pool_vcores,
    session_vcores,
    spark_cu_seconds,
)

MEDIUM_ONE_EXECUTOR = {
    "nodeSize": "Medium",
    "autoScale": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 2},
    "dynamicExecutorAllocation": {"enabled": True, "minExecutors": 1, "maxExecutors": 1},
}
MEDIUM_TWO_EXECUTORS = {
    "nodeSize": "Medium",
    "autoScale": {"enabled": True, "minNodeCount": 3, "maxNodeCount": 3},
    "dynamicExecutorAllocation": {"enabled": True, "minExecutors": 1, "maxExecutors": 2},
}


def test_medium_pool_one_executor_is_16_vcores(rates):
    # 8 vCores for the driver node + 8 for one executor node.
    assert pool_vcores(MEDIUM_ONE_EXECUTOR, rates) == 16


def test_medium_pool_two_executors_is_24_vcores(rates):
    assert pool_vcores(MEDIUM_TWO_EXECUTORS, rates) == 24


def test_vcores_convert_to_cu_at_two_to_one(rates):
    assert spark_cu_seconds(1.0, 24, rates) == 12.0
    assert spark_cu_seconds(1.0, 16, rates) == 8.0


def test_spark_cu_for_a_real_session(rates):
    # A 978-second session on a 24-vCore pool: 12 CU * 978 s.
    assert spark_cu_seconds(978, 24, rates) == pytest.approx(11736.0)


def test_unknown_node_size_falls_back_to_the_default(rates):
    assert pool_vcores({"nodeSize": "Enormous"}, rates, default=16) == 16
    assert pool_vcores(None, rates, default=16) == 16


def test_node_size_matching_is_case_insensitive(rates):
    assert pool_vcores({"nodeSize": "medium", "dynamicExecutorAllocation": {}}, rates) == 16


def test_pool_without_executor_allocation_uses_node_count(rates):
    pool = {"nodeSize": "Small", "autoScale": {"maxNodeCount": 4}}
    # 4 vCores per node, one driver plus three executors.
    assert pool_vcores(pool, rates) == 16


def test_session_fields_win_when_fabric_populates_them(rates):
    session = {"driverCores": 4, "executorCores": 4, "numExecutors": 3}
    assert session_vcores(session, MEDIUM_TWO_EXECUTORS, rates) == 16


def test_session_falls_back_to_pool_when_fields_absent(rates):
    # This is the live behaviour today: the API omits the core fields entirely.
    assert session_vcores({"state": "Succeeded"}, MEDIUM_TWO_EXECUTORS, rates) == 24


def test_dynamic_allocation_max_overrides_num_executors(rates):
    session = {
        "driverCores": 8,
        "executorCores": 8,
        "numExecutors": 1,
        "isDynamicAllocationEnabled": True,
        "dynamicAllocationMaxExecutors": 3,
    }
    assert session_vcores(session, None, rates) == 32


def test_zero_and_negative_durations_cost_nothing(rates):
    assert spark_cu_seconds(0, 24, rates) == 0.0
    assert spark_cu_seconds(-5, 24, rates) == 0.0
    assert spark_cu_seconds(100, 0, rates) == 0.0


def test_orchestration_is_charged_per_activity_run(rates):
    # 0.0056 CU-hours -> 20.16 CU-seconds.
    assert orchestration_cu_seconds(1, rates) == pytest.approx(20.16)
    assert orchestration_cu_seconds(36, rates) == pytest.approx(725.76)
    assert orchestration_cu_seconds(0, rates) == 0.0


def test_dataflow_first_tier_boundary(rates):
    # 600 s exactly at 12 CU/s, and not a second of it at the cheaper rate.
    assert dataflow_cu_seconds(600, rates) == pytest.approx(7200.0)


def test_dataflow_second_tier_applies_only_to_the_overflow(rates):
    # 600 s * 12 + 600 s * 1.5
    assert dataflow_cu_seconds(1200, rates) == pytest.approx(8100.0)


def test_non_cicd_dataflow_is_flat_rate(rates):
    assert dataflow_cu_seconds(100, rates, cicd=False) == pytest.approx(1600.0)


def test_staging_adds_high_scale_compute(rates):
    plain = dataflow_cu_seconds(100, rates)
    staged = dataflow_cu_seconds(100, rates, staging=True)
    assert staged - plain == pytest.approx(600.0)  # 100 s * 6 CU/s
