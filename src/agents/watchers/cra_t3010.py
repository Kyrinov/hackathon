from __future__ import annotations

from .base import BaseWatcher


class CraT3010Watcher(BaseWatcher):
    """CRA T3010 directors/officers — latest fiscal year (2024)."""

    name = "cra_t3010"
    resource_id = "3eb35dcd-9b0c-4ae9-a45c-e5e481567c23"
    external_id_field = "_id"
    fetch_limit = 500
