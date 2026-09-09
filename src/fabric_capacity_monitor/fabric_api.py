"""Thin client over the Fabric REST APIs this tool reads.

Everything here is read-only, and every endpoint used is available to a workspace
*viewer* — no capacity-admin role and no Power BI Pro licence.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import requests

from .auth import FABRIC_RESOURCE, TokenProvider

BASE = "https://api.fabric.microsoft.com/v1"

_RETRY_STATUS = (429, 500, 502, 503, 504)
_MAX_ATTEMPTS = 4


class FabricError(RuntimeError):
    pass


class FabricClient:
    def __init__(self, tokens: TokenProvider, *, timeout: float = 60.0) -> None:
        self._tokens = tokens
        self._timeout = timeout
        self._session = requests.Session()

    # -- plumbing ------------------------------------------------------------
    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            headers = {"Authorization": f"Bearer {self._tokens.token(FABRIC_RESOURCE)}"}
            headers.update(kwargs.pop("headers", {}))
            response = self._session.request(
                method, url, headers=headers, timeout=self._timeout, **kwargs
            )
            if response.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                wait = float(response.headers.get("Retry-After", 2**attempt))
                time.sleep(min(wait, 30.0))
                continue
            return response
        return response  # pragma: no cover - loop always returns

    def get(self, path: str) -> dict:
        url = path if path.startswith("http") else f"{BASE}{path}"
        response = self._request("GET", url)
        if response.status_code == 200:
            return response.json()
        if response.status_code in (401, 403):
            raise FabricError(f"not authorised for {url} (HTTP {response.status_code})")
        raise FabricError(f"GET {url} failed: HTTP {response.status_code} {response.text[:200]}")

    def paged(self, path: str) -> Iterator[dict]:
        """Yield every item across a paginated collection, following continuation links."""
        url: str | None = path
        seen = 0
        while url:
            payload = self.get(url)
            yield from payload.get("value", [])
            seen += len(payload.get("value", []))
            url = payload.get("continuationUri")
            if url and seen > 100_000:  # guard against a pathological loop
                break

    # -- endpoints -----------------------------------------------------------
    def capacities(self) -> list[dict]:
        """Capacities visible to the caller: id, displayName, sku, region, state.

        This is the authoritative source for the capacity GUID that workspaces refer
        to. ARM knows the same capacities by resource name but does not expose that
        GUID, so ARM alone cannot be joined to workspaces.
        """
        return list(self.paged("/capacities"))

    def workspaces(self) -> list[dict]:
        return list(self.paged("/workspaces"))

    def workspaces_for_capacity(self, capacity_id: str) -> list[dict]:
        target = capacity_id.lower()
        return [w for w in self.workspaces() if (w.get("capacityId") or "").lower() == target]

    def spark_pools(self, workspace_id: str) -> list[dict]:
        try:
            return list(self.paged(f"/workspaces/{workspace_id}/spark/pools"))
        except FabricError:
            return []

    def default_pool(self, workspace_id: str) -> dict | None:
        """The pool sessions actually land on: the workspace default, else the starter pool."""
        pools = self.spark_pools(workspace_id)
        if not pools:
            return None
        try:
            settings = self.get(f"/workspaces/{workspace_id}/spark/settings")
            wanted = ((settings.get("pool") or {}).get("defaultPool") or {}).get("name")
        except FabricError:
            wanted = None
        if wanted:
            for pool in pools:
                if pool.get("name") == wanted:
                    return pool
        for pool in pools:
            if pool.get("name") == "Starter Pool":
                return pool
        return pools[0]

    def spark_settings(self, workspace_id: str) -> dict:
        try:
            return self.get(f"/workspaces/{workspace_id}/spark/settings")
        except FabricError:
            return {}

    def livy_sessions(self, workspace_id: str) -> list[dict]:
        try:
            return list(self.paged(f"/workspaces/{workspace_id}/spark/livySessions"))
        except FabricError:
            return []

    def items(self, workspace_id: str) -> list[dict]:
        try:
            return list(self.paged(f"/workspaces/{workspace_id}/items"))
        except FabricError:
            return []

    def job_instances(self, workspace_id: str, item_id: str) -> list[dict]:
        try:
            return list(self.paged(f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances"))
        except FabricError:
            return []
