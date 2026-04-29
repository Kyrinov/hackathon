from __future__ import annotations

from .base import BaseWatcher


class FedGrantsWatcher(BaseWatcher):
    name = "fed_grants"
    resource_id = "1d15a62f-5656-49ad-8c88-f40ce689d831"
    external_id_field = "_id"
    fetch_limit = 500
