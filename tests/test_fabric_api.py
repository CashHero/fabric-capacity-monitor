"""Fabric client plumbing: retries, and outages that must not look like empty results."""

import pytest
import requests

from fabric_capacity_monitor import fabric_api
from fabric_capacity_monitor.fabric_api import FabricClient, FabricUnreachable


class _Tokens:
    def token(self, resource):
        return "t"


class _Response:
    status_code = 200
    headers: dict = {}

    def json(self):
        return {"value": [{"id": "s1"}]}


def _client(monkeypatch, *outcomes):
    """A client whose requests produce ``outcomes`` in turn: an exception or a response."""
    monkeypatch.setattr(fabric_api.time, "sleep", lambda seconds: None)
    client = FabricClient(_Tokens())
    remaining = list(outcomes)

    def request(*args, **kwargs):
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client._session.request = request
    return client


def test_a_transient_timeout_is_retried(monkeypatch):
    client = _client(monkeypatch, requests.ReadTimeout("slow"), _Response())
    assert client.get("/capacities") == {"value": [{"id": "s1"}]}


def test_a_persistent_outage_raises_after_the_last_attempt(monkeypatch):
    client = _client(monkeypatch, *[requests.ConnectionError("down")] * fabric_api._MAX_ATTEMPTS)
    with pytest.raises(FabricUnreachable, match="down"):
        client.get("/capacities")


def test_an_outage_is_not_mistaken_for_an_empty_workspace(monkeypatch):
    # livy_sessions tolerates a refused request as "no sessions"; a timeout must not be.
    client = _client(monkeypatch, *[requests.ReadTimeout("slow")] * fabric_api._MAX_ATTEMPTS)
    with pytest.raises(FabricUnreachable):
        client.livy_sessions("w")
