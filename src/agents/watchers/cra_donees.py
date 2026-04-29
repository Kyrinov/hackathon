from __future__ import annotations

from .base import BaseWatcher


class CraDoneesWatcher(BaseWatcher):
    """CRA T3010 qualified donees — latest fiscal year (2024)."""

    name = "cra_donees"
    resource_id = "e945d3ac-ce8c-40c9-a322-47f477d6a8de"
    external_id_field = "_id"
    fetch_limit = 500
