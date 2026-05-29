"""Catalog connection config (Nessie URL normalization), mirrored from the runner."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NessieConfig:
    """Nessie connection — exposes the Iceberg REST + v2 API URLs."""

    url: str = "http://nessie:19120/api/v1"

    @classmethod
    def from_env(cls) -> NessieConfig:
        return cls(url=os.environ.get("NESSIE_URL", "http://nessie:19120/api/v1"))

    @property
    def _host_url(self) -> str:
        url = self.url.rstrip("/")
        for suffix in ("/api/v1", "/api/v2", "/iceberg"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    @property
    def base_url(self) -> str:
        """Iceberg REST catalog URI (e.g. http://nessie:19120/iceberg)."""
        return self._host_url + "/iceberg"

    @property
    def api_v2_url(self) -> str:
        """Nessie v2 REST API base URL for branch operations."""
        return self._host_url + "/api/v2"
