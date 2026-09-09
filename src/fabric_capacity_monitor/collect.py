"""Turn Fabric API responses into normalised :class:`Operation` records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .cu import orchestration_cu_seconds, session_vcores, spark_cu_seconds
from .fabric_api import FabricClient
from .model import BACKGROUND, ESTIMATED, EXACT, Capacity, Operation, parse_fabric_time
from .rates import Rates

# Fabric reports Livy durations with an explicit unit rather than a fixed one.
_UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}


def duration_seconds(value: dict | None) -> float:
    if not value:
        return 0.0
    unit = str(value.get("timeUnit", "Seconds")).lower()
    return float(value.get("value", 0) or 0) * _UNIT_SECONDS.get(unit, 1)


def _principal(session: dict) -> tuple[str | None, str | None]:
    principal = session.get("submitter") or session.get("consumerIdentity") or {}
    details = principal.get("userDetails") or {}
    name = (
        principal.get("displayName")
        or details.get("userPrincipalName")
        or principal.get("id")
    )
    return name, principal.get("type")


@dataclass
class Collection:
    """Everything gathered for one capacity, plus whatever we could not account for."""

    capacity: Capacity
    operations: list[Operation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unaccounted: list[str] = field(default_factory=list)
    pool_by_workspace: dict[str, dict | None] = field(default_factory=dict)

    @property
    def high_concurrency_present(self) -> bool:
        return any(op.is_high_concurrency for op in self.operations)


def collect_spark(
    client: FabricClient,
    capacity: Capacity,
    rates: Rates,
    since: datetime,
    collection: Collection,
) -> None:
    """Spark sessions across every workspace on the capacity. This is the exact tier."""
    for workspace in capacity.workspaces:
        ws_id = workspace["id"]
        ws_name = workspace.get("displayName", ws_id)
        pool = client.default_pool(ws_id)
        collection.pool_by_workspace[ws_name] = pool
        if pool is None:
            collection.warnings.append(
                f"{ws_name}: no Spark pool visible; assuming 16 vCores per session"
            )
        for session in client.livy_sessions(ws_id):
            end = parse_fabric_time(session.get("endDateTime"))
            if end is None or end < since:
                continue
            running = duration_seconds(session.get("runningDuration"))
            vcores = session_vcores(session, pool, rates)
            user, user_type = _principal(session)
            collection.operations.append(
                Operation(
                    source="spark",
                    exactness=EXACT,
                    workspace_id=ws_id,
                    workspace_name=ws_name,
                    item_id=(session.get("item") or {}).get("itemId"),
                    item_name=session.get("itemName") or "(unnamed)",
                    item_kind=(session.get("itemType") or "Notebook").title(),
                    operation_name=session.get("operationName") or "Spark run",
                    status=session.get("state") or "Unknown",
                    start=parse_fabric_time(session.get("startDateTime")),
                    end=end,
                    duration_seconds=running,
                    cu_seconds=spark_cu_seconds(running, vcores, rates),
                    utilization_type=BACKGROUND,
                    queued_seconds=duration_seconds(session.get("queuedDuration")),
                    user=user,
                    user_type=user_type,
                    is_high_concurrency=bool(session.get("isHighConcurrency")),
                    session_id=session.get("livyId") or session.get("sparkApplicationId"),
                    job_instance_id=session.get("jobInstanceId"),
                )
            )


def collect_pipelines(
    client: FabricClient,
    capacity: Capacity,
    rates: Rates,
    since: datetime,
    collection: Collection,
) -> None:
    """Data pipeline runs.

    Run counts, durations and statuses are exact. The CU figure is a *lower bound*:
    orchestration is charged per activity run, and no public API exposes the activity
    count for a run, so one activity per pipeline run is assumed.
    """
    charged = False
    for workspace in capacity.workspaces:
        ws_id = workspace["id"]
        ws_name = workspace.get("displayName", ws_id)
        pipelines = [i for i in client.items(ws_id) if i.get("type") == "DataPipeline"]
        for pipeline in pipelines:
            for run in client.job_instances(ws_id, pipeline["id"]):
                end = parse_fabric_time(run.get("endTimeUtc"))
                start = parse_fabric_time(run.get("startTimeUtc"))
                if end is None or end < since:
                    continue
                charged = True
                span = (end - start).total_seconds() if start else 0.0
                collection.operations.append(
                    Operation(
                        source="pipeline",
                        exactness=ESTIMATED,
                        workspace_id=ws_id,
                        workspace_name=ws_name,
                        item_id=pipeline["id"],
                        item_name=pipeline.get("displayName", "(pipeline)"),
                        item_kind="DataPipeline",
                        operation_name=f"Pipeline {run.get('invokeType', 'Run')}",
                        status=run.get("status") or "Unknown",
                        start=start,
                        end=end,
                        duration_seconds=span,
                        cu_seconds=orchestration_cu_seconds(1, rates),
                        utilization_type=BACKGROUND,
                        job_instance_id=run.get("id"),
                        note="lower bound: per-activity counts are not exposed by the API",
                    )
                )
    if charged:
        collection.warnings.append(
            "Pipeline CU is a lower bound — orchestration bills per activity run, and no "
            "public API exposes activity counts (queryactivityruns returns 404)."
        )


def collect(
    client: FabricClient,
    capacity: Capacity,
    rates: Rates,
    since: datetime,
    *,
    include_pipelines: bool = True,
) -> Collection:
    collection = Collection(capacity=capacity)
    collect_spark(client, capacity, rates, since, collection)
    if include_pipelines:
        collect_pipelines(client, capacity, rates, since, collection)

    dataflows = sum(
        1
        for workspace in capacity.workspaces
        for item in client.items(workspace["id"])
        if item.get("type") == "Dataflow"
    )
    if dataflows:
        collection.unaccounted.append(
            f"Dataflow Gen2 ({dataflows} item(s)) — refreshes triggered from inside a pipeline "
            "register no job instances, so no duration is available to price."
        )
    collection.unaccounted.append(
        "OneLake transactions, SQL analytics endpoint queries, semantic models and "
        "Eventhouse uptime — no free API exposes their CU."
    )
    if collection.high_concurrency_present:
        collection.warnings.append(
            "High-concurrency sessions detected: Fabric reports one session per stage, named "
            "after the notebook that opened it. Notebooks that joined an existing session have "
            "no row of their own, so per-item CU is per-SESSION, not per-notebook. "
            "Capacity totals are unaffected."
        )
    return collection
