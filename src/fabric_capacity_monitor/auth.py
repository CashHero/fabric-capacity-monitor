"""Token acquisition, pluggable across the ways people actually sign in to Azure."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import requests

FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
ARM_RESOURCE = "https://management.azure.com"
KUSTO_RESOURCE = "https://api.kusto.windows.net"
LOG_ANALYTICS_RESOURCE = "https://api.loganalytics.io"

_METHODS = ("azure-cli", "default", "env")


class AuthError(RuntimeError):
    """Raised when no usable credential could be obtained."""


class TokenProvider:
    """Caches one token per resource for the life of a run."""

    def __init__(self, method: str = "azure-cli") -> None:
        if method not in _METHODS:
            raise AuthError(f"unknown auth method {method!r}; expected one of {_METHODS}")
        self.method = method
        self._cache: dict[str, tuple[str, float]] = {}

    def token(self, resource: str) -> str:
        cached = self._cache.get(resource)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        token, expires = self._acquire(resource)
        self._cache[resource] = (token, expires)
        return token

    def _acquire(self, resource: str) -> tuple[str, float]:
        if self.method == "azure-cli":
            return self._from_cli(resource)
        if self.method == "default":
            return self._from_default(resource)
        return self._from_env(resource)

    @staticmethod
    def _from_cli(resource: str) -> tuple[str, float]:
        if not shutil.which("az"):
            raise AuthError("the Azure CLI ('az') is not on PATH; try --auth default")
        try:
            raw = subprocess.run(
                ["az", "account", "get-access-token", "--resource", resource, "-o", "json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            ).stdout
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip().splitlines()
            hint = detail[-1] if detail else "no detail"
            raise AuthError(f"az could not get a token for {resource}: {hint}\nTry: az login") from exc
        except subprocess.TimeoutExpired as exc:
            raise AuthError(f"az timed out getting a token for {resource}") from exc
        payload = json.loads(raw)
        return payload["accessToken"], time.time() + 3000

    @staticmethod
    def _from_default(resource: str) -> tuple[str, float]:
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise AuthError(
                "--auth default needs azure-identity; install fabric-capacity-monitor[azure]"
            ) from exc
        token = DefaultAzureCredential().get_token(f"{resource}/.default")
        return token.token, float(token.expires_on)

    @staticmethod
    def _from_env(resource: str) -> tuple[str, float]:
        tenant = os.environ.get("AZURE_TENANT_ID")
        client = os.environ.get("AZURE_CLIENT_ID")
        secret = os.environ.get("AZURE_CLIENT_SECRET")
        missing = [
            name
            for name, value in (
                ("AZURE_TENANT_ID", tenant),
                ("AZURE_CLIENT_ID", client),
                ("AZURE_CLIENT_SECRET", secret),
            )
            if not value
        ]
        if missing:
            raise AuthError(f"--auth env needs {', '.join(missing)} in the environment")
        response = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client,
                "client_secret": secret,
                "scope": f"{resource}/.default",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise AuthError(f"client-credentials token request failed: HTTP {response.status_code}")
        payload = response.json()
        return payload["access_token"], time.time() + float(payload.get("expires_in", 3599))
