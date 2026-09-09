"""Split a shared Spark session's CU across the runs that actually happened inside it.

Under high concurrency Fabric reports one session per stage, attributed to whichever
notebook opened it; the notebooks that joined it leave no trace in the Spark API. The
session's CU is therefore correct in total but wrong in attribution.

A *resolver* closes that gap: given a session id, it returns the runs inside it and how
long each took, and :func:`apply_subruns` redistributes the session's CU pro rata. Any
source of per-notebook durations will do — most people already have one, because the
obvious way to get it is to have notebooks log their own runtime alongside the Livy
session id that Fabric exposes to them at runtime.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from .model import Operation


class SubRun(Protocol):
    """One run that happened inside a shared session."""

    name: str
    duration_seconds: float


class SubRunResolver(Protocol):
    """Maps a Livy session id to the runs it contained."""

    def resolve(self, session_id: str) -> list[SubRun]:  # pragma: no cover - protocol
        ...


def apply_subruns(
    operations: list[Operation], resolver: SubRunResolver
) -> tuple[list[Operation], int]:
    """Replace each high-concurrency session with one operation per run inside it.

    CU is divided in proportion to each run's duration, so the capacity total is
    preserved exactly. Sessions the resolver knows nothing about are left untouched
    rather than guessed at. Returns the new operations and how many sessions were split.
    """
    out: list[Operation] = []
    split = 0
    for operation in operations:
        if not operation.is_high_concurrency or not operation.session_id:
            out.append(operation)
            continue
        try:
            children = resolver.resolve(operation.session_id)
        except Exception:  # noqa: BLE001 - a resolver failure must not lose the session
            out.append(operation)
            continue
        total = sum(max(child.duration_seconds, 0.0) for child in children)
        if not children or total <= 0:
            out.append(operation)
            continue
        split += 1
        for child in children:
            share = max(child.duration_seconds, 0.0) / total
            out.append(
                replace(
                    operation,
                    item_name=child.name,
                    cu_seconds=operation.cu_seconds * share,
                    duration_seconds=child.duration_seconds,
                    note="CU apportioned by duration within a shared session",
                )
            )
    return out, split
