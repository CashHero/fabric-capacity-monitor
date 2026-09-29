"""Azure Resource Manager reads: capacity SKU and state, SKU/pause/resume history, cost.

Fabric capacities are ordinary ARM resources, which is the only place the SKU and the
paused/active state are authoritative. Note that Azure Monitor exposes *no* platform
metrics for ``Microsoft.Fabric/capacities`` — that namespace only exists for the legacy
``Microsoft.PowerBIDedicated/capacities`` — so there is nothing to read there.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .auth import ARM_RESOURCE, TokenProvider
from .model import SkuChange, parse_fabric_time

BASE = "https://management.azure.com"
FABRIC_API_VERSION = "2023-11-01"
COST_API_VERSION = "2023-11-01"
GRAPH_API_VERSION = "2022-10-01"


class ArmClient:
    def __init__(self, tokens: TokenProvider, *, timeout: float = 60.0) -> None:
        self._tokens = tokens
        self._timeout = timeout
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.token(ARM_RESOURCE)}"}

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response | None:
        """The response, or ``None`` if ARM couldn't be reached: it is strictly optional."""
        try:
            return getattr(self._session, method)(
                url, headers=self._headers(), timeout=self._timeout, **kwargs
            )
        except requests.RequestException:
            return None

    def _get(self, url: str) -> dict | None:
        response = self._send("get", url)
        return response.json() if response is not None and response.status_code == 200 else None

    def subscriptions(self) -> list[dict]:
        payload = self._get(f"{BASE}/subscriptions?api-version=2022-12-01")
        return payload.get("value", []) if payload else []

    def capacities(self) -> list[dict]:
        """Every Fabric capacity visible to the signed-in identity, across subscriptions."""
        found: list[dict] = []
        for subscription in self.subscriptions():
            sub_id = subscription.get("subscriptionId")
            if not sub_id:
                continue
            url = (
                f"{BASE}/subscriptions/{sub_id}/providers/Microsoft.Fabric/capacities"
                f"?api-version={FABRIC_API_VERSION}"
            )
            payload = self._get(url)
            if payload:
                found.extend(payload.get("value", []))
        return found

    def activity_log(self, resource_id: str, since_iso: str) -> list[dict]:
        """Pause/resume/scale events for a capacity — the app's 'system events' table.

        Azure retains activity log entries for 90 days.
        """
        sub_id = resource_id.split("/")[2] if resource_id.count("/") > 2 else None
        if not sub_id:
            return []
        filt = f"eventTimestamp ge '{since_iso}' and resourceId eq '{resource_id}'"
        url = (
            f"{BASE}/subscriptions/{sub_id}/providers/Microsoft.Insights/eventtypes/"
            f"management/values?api-version=2015-04-01&$filter={requests.utils.quote(filt)}"
        )
        payload = self._get(url)
        return payload.get("value", []) if payload else []

    def sku_changes(self, resource_id: str) -> list[SkuChange]:
        """Resizes of a capacity, oldest first, from Azure Resource Graph change history.

        The activity log records *that* a capacity was written but not the SKU before or
        after; Resource Graph's ``resourcechanges`` does, for the last 14 days.
        """
        sub_id = resource_id.split("/")[2] if resource_id.count("/") > 2 else None
        if not sub_id:
            return []
        query = (
            "resourcechanges"
            f" | where tostring(properties.targetResourceId) =~ '{resource_id}'"
            " | extend sku = properties.changes['sku.name']"
            " | where isnotnull(sku)"
            " | project at = tostring(properties.changeAttributes.timestamp),"
            " previous = tostring(sku.previousValue), new = tostring(sku.newValue)"
        )
        response = self._send(
            "post",
            f"{BASE}/providers/Microsoft.ResourceGraph/resources?api-version={GRAPH_API_VERSION}",
            json={"query": query, "subscriptions": [sub_id]},
        )
        if response is None or response.status_code != 200:
            return []
        changes = [
            SkuChange(at=parse_fabric_time(row["at"]), previous=row["previous"], new=row["new"])
            for row in response.json().get("data", [])
            if row.get("at") and row.get("previous") and row.get("new")
        ]
        return sorted(changes, key=lambda change: change.at)

    def cost_by_meter(
        self, subscription_id: str, start_iso: str, end_iso: str
    ) -> list[tuple[float, str, str]] | None:
        """Daily-summed cost per meter for Fabric capacities in a subscription.

        The Cost Management query API is aggressively throttled and returns HTTP 429
        readily. Callers get ``None`` rather than an exception: cost is a nice-to-have
        and must never fail a capacity report.
        """
        url = (
            f"{BASE}/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/"
            f"query?api-version={COST_API_VERSION}"
        )
        body: dict[str, Any] = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {"from": start_iso, "to": end_iso},
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [
                    {"type": "Dimension", "name": "MeterSubCategory"},
                    {"type": "Dimension", "name": "Meter"},
                ],
                "filter": {
                    "dimensions": {
                        "name": "ResourceType",
                        "operator": "In",
                        "values": ["microsoft.fabric/capacities"],
                    }
                },
            },
        }
        for attempt in range(3):
            response = self._send("post", url, json=body)
            if response is None:
                return None
            if response.status_code == 200:
                rows = response.json().get("properties", {}).get("rows", [])
                out: list[tuple[float, str, str]] = []
                for row in rows:
                    if len(row) >= 3:
                        out.append((float(row[0]), str(row[1]), str(row[2])))
                return sorted(out, key=lambda item: -item[0])
            if response.status_code == 429 and attempt < 2:
                time.sleep(float(response.headers.get("Retry-After", 20)))
                continue
            return None
        return None
