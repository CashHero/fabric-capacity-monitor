"""ARM reads: optional, so they degrade to empty rather than raising."""

from datetime import UTC, datetime

import requests

from fabric_capacity_monitor.arm import ArmClient
from fabric_capacity_monitor.model import SkuChange

RESOURCE_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Fabric/capacities/cap"
)


class _Tokens:
    def token(self, resource):
        return "t"


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _client(response):
    client = ArmClient(_Tokens())
    client._session.post = lambda *args, **kwargs: response
    return client


def test_sku_changes_are_parsed_oldest_first():
    rows = [
        {"at": "2026-09-29T09:12:38.292Z", "previous": "F4", "new": "F8"},
        {"at": "2026-09-20T08:00:00Z", "previous": "F2", "new": "F4"},
        {"at": "2026-09-21T08:00:00Z", "previous": "", "new": "F4"},  # a create: skipped
    ]
    changes = _client(_Response(200, {"data": rows})).sku_changes(RESOURCE_ID)
    assert changes == [
        SkuChange(at=datetime(2026, 9, 20, 8, tzinfo=UTC), previous="F2", new="F4"),
        SkuChange(
            at=datetime(2026, 9, 29, 9, 12, 38, 292000, tzinfo=UTC), previous="F4", new="F8"
        ),
    ]


def test_sku_changes_degrade_to_empty_without_access():
    assert _client(_Response(403)).sku_changes(RESOURCE_ID) == []


def _unreachable():
    def fail(*args, **kwargs):
        raise requests.ConnectionError("down")

    client = ArmClient(_Tokens())
    client._session.get = fail
    client._session.post = fail
    return client


def test_network_failures_degrade_to_empty_rather_than_raising():
    client = _unreachable()
    assert client.capacities() == []
    assert client.activity_log(RESOURCE_ID, "2026-09-01T00:00:00Z") == []
    assert client.sku_changes(RESOURCE_ID) == []
    assert client.cost_by_meter("sub", "2026-09-01", "2026-09-02") is None
