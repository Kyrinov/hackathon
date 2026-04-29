from __future__ import annotations

from src.agents import scheduler, state, validators


class FakeClient:
    base_url = "https://example.test/datastore_search"


class FakeWatcher:
    name = "fed_grants"
    resource_id = "resource-1"
    external_id_field = "_id"
    client = FakeClient()

    def fetch_new(self):
        return [
            {
                "_id": 101,
                "recipient_legal_name": "Northern Pathways Community Society",
                "recipient_business_number": "123456789",
                "agreement_value": "250000",
            },
            {
                "recipient_legal_name": "Missing Identifier Society",
                "agreement_value": "100000",
            },
        ]


def test_validate_rows_quarantines_missing_external_id():
    rows = [{"_id": 1, "recipient_legal_name": "Valid"}, {"recipient_legal_name": "Invalid"}]

    valid, quarantine = validators.validate_rows("fed_grants", rows, "batch-1")

    assert len(valid) == 1
    assert valid[0]["_batch_id"] == "batch-1"
    assert valid[0]["_row_hash"]
    assert len(quarantine) == 1
    assert quarantine[0]["source"] == "fed_grants"
    assert "Field required" in quarantine[0]["error"]


def test_scheduler_persists_batch_quarantine_and_finding(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def fake_resolve(rows, source):
        assert source == "fed_grants"
        assert len(rows) == 1
        return {
            "101": {
                "entity_ids": [1001],
                "mapping_method": "bn_root",
                "confidence_score": 1.0,
            }
        }

    def fake_analyze(resolved, source, rows_by_external_id):
        assert resolved == {"101": [1001]}
        assert rows_by_external_id["101"]["_row_hash"]
        return [
            {
                "source": source,
                "finding_type": "new_ring",
                "entity_ids": [1001, 1002],
                "ring_id": "cra-cycle-1",
                "trigger_external_id": "101",
                "narrative": "seed",
                "total_amount": 250000.0,
                "severity": "review",
            }
        ]

    monkeypatch.setattr(scheduler.resolver, "resolve_batch_with_metadata", fake_resolve)
    monkeypatch.setattr(scheduler.analyst, "analyze", fake_analyze)

    produced = scheduler.run_cycle([FakeWatcher()], persist=True, brief_findings=False)

    assert produced == 1
    findings = state.list_findings()
    assert len(findings) == 1
    assert findings[0]["batch_id"]
    assert findings[0]["resource_id"] == "resource-1"
    assert findings[0]["trigger_row_hash"]
    assert findings[0]["mapping_method"] == "bn_root"
    assert findings[0]["confidence_score"] == 1.0

    with state.connect() as conn:
        batch = conn.execute("SELECT * FROM staged_batches").fetchone()
        quarantine = conn.execute("SELECT * FROM bronze_quarantine").fetchall()

    assert batch["raw_row_count"] == 2
    assert batch["valid_row_count"] == 1
    assert batch["quarantined_row_count"] == 1
    assert len(quarantine) == 1
