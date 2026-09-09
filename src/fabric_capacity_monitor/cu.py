"""Capacity-unit computation, per workload.

Spark is exact: Fabric bills a fixed number of vCores for the lifetime of a session,
and the vCore-to-CU rate is published. Everything else is an estimate derived from a
published rate and a duration, and is marked as such so a renderer can never present
it as measured.
"""

from __future__ import annotations

from .rates import Rates

# Fabric's own field names for a pool's executor allocation, most specific first.
_EXECUTOR_KEYS = ("maxExecutors", "minExecutors")


def pool_vcores(pool: dict | None, rates: Rates, default: int = 16) -> int:
    """Total vCores a session on ``pool`` occupies: one driver node plus its executors.

    Fabric bills the whole allocation for the session's lifetime, not the portion
    actually busy, so the maximum executor count is the right figure.
    """
    if not pool:
        return default
    per_node = rates.node_vcores(pool.get("nodeSize"))
    if per_node is None:
        return default
    allocation = pool.get("dynamicExecutorAllocation") or {}
    executors = None
    for key in _EXECUTOR_KEYS:
        if allocation.get(key) is not None:
            executors = int(allocation[key])
            break
    if executors is None:
        autoscale = pool.get("autoScale") or {}
        # Fall back to node count, which includes the driver node.
        nodes = autoscale.get("maxNodeCount")
        executors = max(int(nodes) - 1, 0) if nodes else 1
    return per_node * (1 + executors)


def session_vcores(session: dict, pool: dict | None, rates: Rates, default: int = 16) -> int:
    """vCores for one Livy session.

    The API documents ``driverCores``/``executorCores``/``numExecutors``, but does not
    currently populate them, so the workspace pool is the reliable source. Prefer the
    per-session fields whenever Microsoft starts returning them.
    """
    driver = session.get("driverCores")
    executor = session.get("executorCores")
    count = session.get("numExecutors")
    if session.get("isDynamicAllocationEnabled") and session.get("dynamicAllocationMaxExecutors"):
        count = session["dynamicAllocationMaxExecutors"]
    if driver and executor and count is not None:
        return int(driver) + int(executor) * int(count)
    return pool_vcores(pool, rates, default=default)


def spark_cu_seconds(running_seconds: float, vcores: int, rates: Rates) -> float:
    """CU-seconds for a Spark session of ``running_seconds`` on ``vcores`` vCores."""
    if running_seconds <= 0 or vcores <= 0:
        return 0.0
    return (vcores / rates.vcores_per_cu) * running_seconds


def orchestration_cu_seconds(activity_runs: int, rates: Rates) -> float:
    """CU-seconds for pipeline orchestration: a flat charge per non-copy activity run."""
    if activity_runs <= 0:
        return 0.0
    return activity_runs * rates.orchestration_cu_seconds_per_activity


def dataflow_cu_seconds(
    duration_seconds: float,
    rates: Rates,
    *,
    cicd: bool = True,
    staging: bool = False,
) -> float:
    """CU-seconds for a Dataflow Gen2 query evaluation.

    CI/CD dataflows are charged at a high rate for the first ten minutes and a much
    lower one thereafter; non-CI/CD dataflows are flat. ``staging`` adds High Scale
    compute, charged over the same duration.
    """
    if duration_seconds <= 0:
        return 0.0
    conf = rates.dataflow
    if cicd:
        tier_seconds = float(conf["cicd_first_tier_seconds"])
        first = min(duration_seconds, tier_seconds) * float(conf["cicd_first_tier_cu_per_second"])
        overflow = max(duration_seconds - tier_seconds, 0.0)
        total = first + overflow * float(conf["cicd_second_tier_cu_per_second"])
    else:
        total = duration_seconds * float(conf["non_cicd_cu_per_second"])
    if staging:
        total += duration_seconds * float(conf["high_scale_cu_per_second"])
    return total
