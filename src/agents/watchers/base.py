from __future__ import annotations

import time
from typing import Any

import requests

from src.agents import state

CKAN_BASE = "https://open.canada.ca/data/en/api/3/action/datastore_search"


class CKANClient:
    """Paginated, retry-safe CKAN datastore_search client."""

    def __init__(self, base_url: str = CKAN_BASE, timeout: float = 20.0):
        self.base_url = base_url
        self.timeout = timeout

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(5):
            try:
                resp = requests.get(self.base_url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("success"):
                        return body["result"]
                    raise RuntimeError(f"ckan error: {body.get('error')}")
                if resp.status_code in (429, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
            except (requests.RequestException, ValueError):
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("ckan retries exhausted")

    def fetch_page(
        self,
        resource_id: str,
        offset: int = 0,
        limit: int = 1000,
        sort: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {"resource_id": resource_id, "offset": offset, "limit": limit}
        if sort:
            params["sort"] = sort
        result = self._get(params)
        return result.get("records", []), int(result.get("total", 0))

    def fetch_recent(
        self,
        resource_id: str,
        limit: int = 500,
        sort: str = "_id desc",
    ) -> list[dict[str, Any]]:
        records, _ = self.fetch_page(resource_id, offset=0, limit=limit, sort=sort)
        return records


class BaseWatcher:
    """Per-source watcher contract."""

    name: str = ""
    resource_id: str = ""
    external_id_field: str = "_id"
    fetch_limit: int = 500

    def __init__(self, client: CKANClient | None = None):
        self.client = client or CKANClient()

    def fetch_new(self) -> list[dict[str, Any]]:
        state.init_db()
        if not self.resource_id:
            return []
        records = self.client.fetch_recent(self.resource_id, limit=self.fetch_limit)
        if not records:
            return []
        external_ids = [str(r.get(self.external_id_field)) for r in records if r.get(self.external_id_field) is not None]
        unseen = state.filter_unseen(self.name, external_ids)
        new_rows = [r for r in records if str(r.get(self.external_id_field)) in unseen]
        if new_rows:
            state.mark_seen(self.name, [str(r.get(self.external_id_field)) for r in new_rows])
            state.upsert_source_state(
                self.name,
                last_cursor=str(max(int(r.get("_id", 0)) for r in new_rows if r.get("_id") is not None)),
                rows_added=len(new_rows),
            )
        else:
            state.upsert_source_state(self.name, rows_added=0)
        return new_rows
