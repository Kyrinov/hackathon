from __future__ import annotations

from scripts.run_splink_resolution import bn_root, clean_name
from src.agents import resolver, state


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query):
        self._offset = 0

    def fetchmany(self, size):
        batch = self._rows[self._offset : self._offset + size]
        self._offset += size
        return batch


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def _reset_resolver_index():
    resolver._BN_INDEX = None
    resolver._NAME_INDEX = None
    resolver._ALIAS_OVERRIDE_INDEX = None
    resolver._SURVIVOR_OVERRIDES = None
    resolver._ALIAS_OVERRIDE_CONFIDENCE = None


def _fake_golden_rows():
    return [
        {
            "id": 1,
            "bn_root": "111111111",
            "canonical_name": "Acme Legal Foundation",
            "aliases": [],
        },
        {
            "id": 2,
            "bn_root": "222222222",
            "canonical_name": "Acme Operating Co.",
            "aliases": [],
        },
    ]


def test_splink_state_migration_is_idempotent_and_preserves_findings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    state.init_db()
    state.insert_finding("fed_grants", "seed", [1], narrative="keep me")
    state.init_db()

    findings = state.list_findings()
    with state.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert findings[0]["narrative"] == "keep me"
    assert "splink_runs" in tables
    assert "splink_entity_candidates" in tables
    assert "entity_resolution_overrides" in tables


def test_splink_name_and_bn_normalization():
    assert clean_name("Acme Foundation Inc. / Fondation Acme") == "ACME"
    assert clean_name("Northern Pathways O/A Pathways North") == "NORTHERN PATHWAYS"
    assert clean_name("Boreal Futures DBA Boreal Labs") == "BOREAL FUTURES"
    assert clean_name("Example Society Trade Name Of Example") == "EXAMPLE"
    assert clean_name("A.B.C. Community, Ltd.") == "A.B.C. COMMUNITY"
    assert bn_root("123456789 RR 0001") == "123456789"
    assert bn_root("000000000") is None


def test_approved_alias_override_resolves_to_survivor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_resolver_index()
    monkeypatch.setattr(resolver, "get_conn", lambda: _FakeConn(_fake_golden_rows()))

    state.init_db()
    run_id = state.start_splink_run(0.4, "test")
    [candidate] = [
        {
            "entity_id_l": 1,
            "entity_id_r": 2,
            "record_id_l": "entity:1:canonical",
            "record_id_r": "source_link:2",
            "legal_name_l": "Acme Legal Foundation",
            "legal_name_r": "Acme Operating",
            "match_probability": 0.98,
        }
    ]
    state.insert_splink_candidates(run_id, [candidate])
    candidate_id = state.list_splink_candidates(status="likely_same")[0]["id"]
    state.approve_splink_candidate(candidate_id)

    resolved = resolver.resolve_batch_with_metadata(
        [{"_id": "row-1", "recipient_legal_name": "Acme Operating"}],
        "fed_grants",
    )

    assert resolved["row-1"]["entity_ids"] == [1]
    assert resolved["row-1"]["mapping_method"] == "splink_alias_override"
    assert resolved["row-1"]["confidence_score"] == 0.98


def test_survivor_override_collapses_duplicate_bn_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_resolver_index()
    monkeypatch.setattr(resolver, "get_conn", lambda: _FakeConn(_fake_golden_rows()))

    state.init_db()
    run_id = state.start_splink_run(0.4, "test")
    state.insert_splink_candidates(
        run_id,
        [
            {
                "entity_id_l": 1,
                "entity_id_r": 2,
                "record_id_l": "entity:1:canonical",
                "record_id_r": "entity:2:canonical",
                "legal_name_l": "Acme Legal Foundation",
                "legal_name_r": "Acme Operating Co.",
                "match_probability": 0.99,
            }
        ],
    )
    state.approve_splink_candidate(state.list_splink_candidates(status="likely_same")[0]["id"])

    resolved = resolver.resolve_batch_with_metadata(
        [
            {
                "_id": "row-2",
                "recipient_legal_name": "ignored",
                "recipient_business_number": "222222222",
            }
        ],
        "fed_grants",
    )

    assert resolved["row-2"]["entity_ids"] == [1]
    assert resolved["row-2"]["mapping_method"] == "splink_survivor_override"
    assert resolved["row-2"]["confidence_score"] == 0.99


def test_unapproved_splink_candidate_does_not_affect_resolution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_resolver_index()
    monkeypatch.setattr(resolver, "get_conn", lambda: _FakeConn(_fake_golden_rows()))

    state.init_db()
    run_id = state.start_splink_run(0.4, "test")
    state.insert_splink_candidates(
        run_id,
        [
            {
                "entity_id_l": 1,
                "entity_id_r": 2,
                "record_id_l": "entity:1:canonical",
                "record_id_r": "source_link:2",
                "legal_name_l": "Acme Legal Foundation",
                "legal_name_r": "Acme Operating",
                "match_probability": 0.98,
            }
        ],
    )

    resolved = resolver.resolve_batch_with_metadata(
        [{"_id": "row-3", "recipient_legal_name": "Acme Operating"}],
        "fed_grants",
    )

    assert resolved == {}
