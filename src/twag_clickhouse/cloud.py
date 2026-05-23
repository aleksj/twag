from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

CLICKHOUSE_CLOUD_API_BASE = "https://api.clickhouse.cloud/v1"


@dataclass(frozen=True)
class ClickHouseCloudConfig:
    service_id: str
    key_id: str
    key_secret: str
    organization_id: str | None = None
    api_base: str = CLICKHOUSE_CLOUD_API_BASE

    @classmethod
    def from_env(cls, *, env_file: str | None = ".env") -> "ClickHouseCloudConfig":
        if env_file and load_dotenv:
            load_dotenv(env_file, override=False)

        service_id = os.getenv("CLICKHOUSE_SERVICE_ID", "").strip()
        key_id = os.getenv("CLICKHOUSE_CLOUD_KEY_ID", "").strip()
        key_secret = os.getenv("CLICKHOUSE_CLOUD_KEY_SECRET", "").strip()
        organization_id = os.getenv("CLICKHOUSE_ORGANIZATION_ID", "").strip() or None

        if not service_id:
            raise ValueError("CLICKHOUSE_SERVICE_ID is required")
        if not key_id:
            raise ValueError("CLICKHOUSE_CLOUD_KEY_ID is required")
        if not key_secret:
            raise ValueError("CLICKHOUSE_CLOUD_KEY_SECRET is required")

        return cls(
            service_id=service_id,
            key_id=key_id,
            key_secret=key_secret,
            organization_id=organization_id,
        )


class ClickHouseCloudClient:
    def __init__(self, config: ClickHouseCloudConfig):
        self.config = config

    def _request(self, path: str) -> dict[str, Any]:
        url = f"{self.config.api_base}{path}"
        credentials = f"{self.config.key_id}:{self.config.key_secret}".encode("utf-8")
        encoded = base64.b64encode(credentials).decode("ascii")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Basic {encoded}",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ClickHouse Cloud API error {exc.code}: {detail}") from exc

    def list_organizations(self) -> list[dict[str, Any]]:
        payload = self._request("/organizations")
        return payload.get("result", [])

    def organization_id(self) -> str:
        if self.config.organization_id:
            return self.config.organization_id

        organizations = self.list_organizations()
        if len(organizations) != 1:
            raise RuntimeError(
                "CLICKHOUSE_ORGANIZATION_ID is required when the API key can see "
                f"{len(organizations)} organizations"
            )

        organization_id = organizations[0].get("id")
        if not organization_id:
            raise RuntimeError("ClickHouse Cloud organization response did not include an id")
        return organization_id

    def get_service(self) -> dict[str, Any]:
        organization_id = self.organization_id()
        payload = self._request(
            f"/organizations/{organization_id}/services/{self.config.service_id}"
        )
        return payload.get("result", {})

    def get_sql_endpoint(self) -> dict[str, Any]:
        service = self.get_service()
        endpoints = service.get("endpoints") or []

        for protocol in ("https", "http"):
            for endpoint in endpoints:
                if endpoint.get("protocol") == protocol:
                    return endpoint

        if endpoints:
            return endpoints[0]

        raise RuntimeError("ClickHouse Cloud service response did not include endpoints")

    def connection_defaults(self) -> dict[str, Any]:
        endpoint = self.get_sql_endpoint()
        return {
            "service_id": self.config.service_id,
            "host": endpoint.get("host", ""),
            "port": endpoint.get("port", 8443),
            "username": endpoint.get("username", "default"),
            "protocol": endpoint.get("protocol", ""),
        }
