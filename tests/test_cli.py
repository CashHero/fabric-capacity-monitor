"""Command-line behaviour: failures end in an exit code, never a traceback."""

import pytest
import requests

from fabric_capacity_monitor import auth, cli
from fabric_capacity_monitor.auth import AuthError
from fabric_capacity_monitor.fabric_api import FabricUnreachable


@pytest.fixture(autouse=True)
def _no_user_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_an_unreachable_fabric_api_exits_2(monkeypatch, capsys):
    def unreachable(*args, **kwargs):
        raise FabricUnreachable("GET https://api.fabric.microsoft.com/v1/workspaces: timed out")

    monkeypatch.setattr(cli, "cmd_list", unreachable)
    assert cli.main(["list-capacities"]) == 2
    assert "Fabric API unreachable" in capsys.readouterr().err


def test_a_token_endpoint_timeout_is_an_auth_error(monkeypatch):
    for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
        monkeypatch.setenv(name, "x")

    def timeout(*args, **kwargs):
        raise requests.ReadTimeout("slow")

    monkeypatch.setattr(auth.requests, "post", timeout)
    with pytest.raises(AuthError, match="slow"):
        auth.TokenProvider._from_env("https://api.fabric.microsoft.com")
