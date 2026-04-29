from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

DB_PATH = Path("data/agent_state.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_cursor TEXT,
    rows_fetched_total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_external_ids (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    entity_ids TEXT NOT NULL,
    ring_id TEXT,
    trigger_external_id TEXT,
    narrative TEXT,
    total_amount REAL,
    severity TEXT NOT NULL DEFAULT 'info'
);
CREATE TABLE IF NOT EXISTS staged_batches (
    batch_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    resource_id TEXT,
    source_url TEXT,
    fetched_at TEXT NOT NULL,
    raw_row_count INTEGER NOT NULL,
    valid_row_count INTEGER NOT NULL DEFAULT 0,
    quarantined_row_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bronze_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    row_hash TEXT NOT NULL,
    raw_row TEXT NOT NULL,
    error TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_created ON findings(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_findings_source ON findings(source);
CREATE INDEX IF NOT EXISTS idx_quarantine_batch ON bronze_quarantine(batch_id);
CREATE TABLE IF NOT EXISTS splink_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    splink_version TEXT,
    threshold REAL NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    cluster_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    config_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE TABLE IF NOT EXISTS splink_entity_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    entity_id_l INTEGER,
    entity_id_r INTEGER,
    record_id_l TEXT NOT NULL,
    record_id_r TEXT NOT NULL,
    legal_name_l TEXT,
    legal_name_r TEXT,
    cleaned_name_l TEXT,
    cleaned_name_r TEXT,
    bn_root_l TEXT,
    bn_root_r TEXT,
    city_l TEXT,
    city_r TEXT,
    province_l TEXT,
    province_r TEXT,
    source_dataset_l TEXT,
    source_dataset_r TEXT,
    source_count_l INTEGER NOT NULL DEFAULT 0,
    source_count_r INTEGER NOT NULL DEFAULT 0,
    match_probability REAL NOT NULL,
    match_weight REAL,
    method TEXT NOT NULL DEFAULT 'splink',
    status TEXT NOT NULL DEFAULT 'needs_review',
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_note TEXT,
    FOREIGN KEY(run_id) REFERENCES splink_runs(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_splink_candidates_pair
    ON splink_entity_candidates(run_id, record_id_l, record_id_r);
CREATE INDEX IF NOT EXISTS idx_splink_candidates_status_prob
    ON splink_entity_candidates(status, match_probability DESC);
CREATE TABLE IF NOT EXISTS entity_resolution_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'approved',
    alias TEXT,
    alias_norm TEXT,
    entity_id INTEGER,
    duplicate_entity_id INTEGER,
    survivor_entity_id INTEGER,
    source_candidate_id INTEGER,
    confidence REAL NOT NULL DEFAULT 1.0,
    notes TEXT,
    FOREIGN KEY(source_candidate_id) REFERENCES splink_entity_candidates(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_override_alias
    ON entity_resolution_overrides(alias_norm, entity_id)
    WHERE status = 'approved' AND alias_norm IS NOT NULL AND entity_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_override_survivor
    ON entity_resolution_overrides(duplicate_entity_id)
    WHERE status = 'approved' AND duplicate_entity_id IS NOT NULL;
"""

_FINDING_COLUMNS = {
    "batch_id": "TEXT",
    "resource_id": "TEXT",
    "source_url": "TEXT",
    "fetched_at": "TEXT",
    "trigger_row_hash": "TEXT",
    "mapping_method": "TEXT",
    "confidence_score": "REAL",
    "review_status": "TEXT NOT NULL DEFAULT 'pending'",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(findings)").fetchall()
        }
        for column, ddl in _FINDING_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE findings ADD COLUMN {column} {ddl}")


def start_splink_run(
    threshold: float,
    splink_version: str | None,
    config: dict[str, Any] | None = None,
    db_path: Path | str = DB_PATH,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO splink_runs(started_at, splink_version, threshold, config_json, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (_utcnow_iso(), splink_version, float(threshold), json.dumps(config or {}, sort_keys=True)),
        )
        return int(cur.lastrowid)


def finish_splink_run(
    run_id: int,
    record_count: int,
    candidate_count: int,
    cluster_count: int = 0,
    status: str = "completed",
    error: str | None = None,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE splink_runs
            SET completed_at = ?, record_count = ?, candidate_count = ?, cluster_count = ?,
                status = ?, error = ?
            WHERE id = ?
            """,
            (
                _utcnow_iso(),
                int(record_count),
                int(candidate_count),
                int(cluster_count),
                status,
                error,
                int(run_id),
            ),
        )


def insert_splink_candidates(
    run_id: int,
    candidates: Iterable[dict[str, Any]],
    db_path: Path | str = DB_PATH,
) -> int:
    rows = []
    now = _utcnow_iso()
    for item in candidates:
        probability = float(item.get("match_probability") or item.get("probability") or 0.0)
        status = item.get("status") or (
            "likely_same" if probability >= 0.97 else "needs_review" if probability >= 0.70 else "audit"
        )
        rows.append(
            (
                int(run_id),
                now,
                item.get("entity_id_l"),
                item.get("entity_id_r"),
                str(item["record_id_l"]),
                str(item["record_id_r"]),
                item.get("legal_name_l"),
                item.get("legal_name_r"),
                item.get("cleaned_name_l"),
                item.get("cleaned_name_r"),
                item.get("bn_root_l"),
                item.get("bn_root_r"),
                item.get("city_l"),
                item.get("city_r"),
                item.get("province_l"),
                item.get("province_r"),
                item.get("source_dataset_l"),
                item.get("source_dataset_r"),
                int(item.get("source_count_l") or 0),
                int(item.get("source_count_r") or 0),
                probability,
                item.get("match_weight"),
                item.get("method") or "splink",
                status,
            )
        )
    if not rows:
        return 0
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO splink_entity_candidates(
                run_id, created_at, entity_id_l, entity_id_r, record_id_l, record_id_r,
                legal_name_l, legal_name_r, cleaned_name_l, cleaned_name_r,
                bn_root_l, bn_root_r, city_l, city_r, province_l, province_r,
                source_dataset_l, source_dataset_r, source_count_l, source_count_r,
                match_probability, match_weight, method, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def list_splink_candidates(
    status: str | None = None,
    limit: int = 200,
    db_path: Path | str = DB_PATH,
) -> list[dict[str, Any]]:
    init_db(db_path)
    query = "SELECT * FROM splink_entity_candidates"
    params: list[Any] = []
    if status and status != "all":
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY match_probability DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def update_splink_candidate_status(
    candidate_id: int,
    status: str,
    reviewed_by: str | None = None,
    review_note: str | None = None,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE splink_entity_candidates
            SET status = ?, reviewed_at = ?, reviewed_by = ?, review_note = ?
            WHERE id = ?
            """,
            (status, _utcnow_iso(), reviewed_by, review_note, int(candidate_id)),
        )


def approve_splink_candidate(
    candidate_id: int,
    reviewed_by: str | None = None,
    review_note: str | None = None,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM splink_entity_candidates WHERE id = ?",
            (int(candidate_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown Splink candidate id: {candidate_id}")
        candidate = dict(row)
        now = _utcnow_iso()
        confidence = float(candidate.get("match_probability") or 0.0)
        left_id = candidate.get("entity_id_l")
        right_id = candidate.get("entity_id_r")
        survivor = duplicate = None
        if left_id and right_id and int(left_id) != int(right_id):
            if int(candidate.get("source_count_l") or 0) > int(candidate.get("source_count_r") or 0):
                survivor, duplicate = int(left_id), int(right_id)
            elif int(candidate.get("source_count_r") or 0) > int(candidate.get("source_count_l") or 0):
                survivor, duplicate = int(right_id), int(left_id)
            else:
                survivor, duplicate = sorted([int(left_id), int(right_id)])
            cur = conn.execute(
                """
                UPDATE entity_resolution_overrides
                SET updated_at = ?, survivor_entity_id = ?, source_candidate_id = ?,
                    confidence = ?, notes = ?
                WHERE status = 'approved' AND duplicate_entity_id = ?
                """,
                (now, survivor, int(candidate_id), confidence, review_note, duplicate),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO entity_resolution_overrides(
                        created_at, updated_at, duplicate_entity_id, survivor_entity_id,
                        source_candidate_id, confidence, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (now, now, duplicate, survivor, int(candidate_id), confidence, review_note),
                )
        for alias, entity_id in (
            (candidate.get("legal_name_l"), right_id or survivor),
            (candidate.get("legal_name_r"), left_id or survivor),
        ):
            if not alias or not entity_id:
                continue
            alias_norm = _simple_norm(alias)
            if not alias_norm:
                continue
            cur = conn.execute(
                """
                UPDATE entity_resolution_overrides
                SET updated_at = ?, alias = ?, source_candidate_id = ?, confidence = ?, notes = ?
                WHERE status = 'approved' AND alias_norm = ? AND entity_id = ?
                """,
                (now, str(alias), int(candidate_id), confidence, review_note, alias_norm, int(entity_id)),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO entity_resolution_overrides(
                        created_at, updated_at, alias, alias_norm, entity_id,
                        source_candidate_id, confidence, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        now,
                        str(alias),
                        alias_norm,
                        int(entity_id),
                        int(candidate_id),
                        confidence,
                        review_note,
                    ),
                )
        conn.execute(
            """
            UPDATE splink_entity_candidates
            SET status = 'same', reviewed_at = ?, reviewed_by = ?, review_note = ?
            WHERE id = ?
            """,
            (now, reviewed_by, review_note, int(candidate_id)),
        )


def load_resolution_overrides(
    db_path: Path | str = DB_PATH,
) -> dict[str, list[dict[str, Any]]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = [dict(row) for row in conn.execute(
            """
            SELECT alias_norm, entity_id, duplicate_entity_id, survivor_entity_id,
                   source_candidate_id, confidence
            FROM entity_resolution_overrides
            WHERE status = 'approved'
            """
        )]
    alias_rows = [row for row in rows if row.get("alias_norm") and row.get("entity_id")]
    survivor_rows = [
        row for row in rows if row.get("duplicate_entity_id") and row.get("survivor_entity_id")
    ]
    return {"aliases": alias_rows, "survivors": survivor_rows}


def _simple_norm(value: Any) -> str | None:
    import re

    s = re.sub(r"[^a-z0-9 ]", " ", str(value).lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) >= 3 else None


def get_source_state(name: str, db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def upsert_source_state(
    name: str,
    last_cursor: str | None = None,
    rows_added: int = 0,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sources(name, last_run_at, last_cursor, rows_fetched_total)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                last_run_at = excluded.last_run_at,
                last_cursor = COALESCE(excluded.last_cursor, sources.last_cursor),
                rows_fetched_total = sources.rows_fetched_total + ?
            """,
            (name, _utcnow_iso(), last_cursor, rows_added, rows_added),
        )


def filter_unseen(
    source: str,
    external_ids: Iterable[str],
    db_path: Path | str = DB_PATH,
) -> set[str]:
    ids = [str(eid) for eid in external_ids if eid is not None]
    if not ids:
        return set()
    with connect(db_path) as conn:
        placeholders = ",".join("?" for _ in ids)
        seen = {
            row["external_id"]
            for row in conn.execute(
                f"SELECT external_id FROM seen_external_ids WHERE source = ? AND external_id IN ({placeholders})",
                (source, *ids),
            )
        }
    return set(ids) - seen


def mark_seen(
    source: str,
    external_ids: Iterable[str],
    db_path: Path | str = DB_PATH,
) -> None:
    rows = [(source, str(eid)) for eid in external_ids if eid is not None]
    if not rows:
        return
    with connect(db_path) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_external_ids(source, external_id) VALUES (?, ?)",
            rows,
        )


def insert_finding(
    source: str,
    finding_type: str,
    entity_ids: list[int | str],
    narrative: str | None = None,
    ring_id: str | None = None,
    trigger_external_id: str | None = None,
    total_amount: float | None = None,
    severity: str = "info",
    batch_id: str | None = None,
    resource_id: str | None = None,
    source_url: str | None = None,
    fetched_at: str | None = None,
    trigger_row_hash: str | None = None,
    mapping_method: str | None = None,
    confidence_score: float | None = None,
    review_status: str = "pending",
    db_path: Path | str = DB_PATH,
) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO findings(
                created_at, source, finding_type, entity_ids, ring_id,
                trigger_external_id, narrative, total_amount, severity,
                batch_id, resource_id, source_url, fetched_at, trigger_row_hash,
                mapping_method, confidence_score, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utcnow_iso(),
                source,
                finding_type,
                json.dumps([str(eid) for eid in entity_ids]),
                ring_id,
                trigger_external_id,
                narrative,
                total_amount,
                severity,
                batch_id,
                resource_id,
                source_url,
                fetched_at,
                trigger_row_hash,
                mapping_method,
                confidence_score,
                review_status,
            ),
        )
        return int(cur.lastrowid)


def insert_staged_batch(
    batch_id: str,
    source: str,
    resource_id: str | None,
    source_url: str | None,
    fetched_at: str,
    raw_row_count: int,
    valid_row_count: int,
    quarantined_row_count: int,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO staged_batches(
                batch_id, source, resource_id, source_url, fetched_at,
                raw_row_count, valid_row_count, quarantined_row_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                source,
                resource_id,
                source_url,
                fetched_at,
                raw_row_count,
                valid_row_count,
                quarantined_row_count,
            ),
        )


def insert_quarantine(
    rows: list[dict[str, Any]],
    db_path: Path | str = DB_PATH,
) -> int:
    if not rows:
        return 0
    payload = [
        (
            _utcnow_iso(),
            row["batch_id"],
            row["source"],
            row.get("external_id"),
            row["row_hash"],
            json.dumps(row.get("raw_row", {}), sort_keys=True, default=str),
            row["error"],
        )
        for row in rows
    ]
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO bronze_quarantine(
                created_at, batch_id, source, external_id, row_hash, raw_row, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
    return len(payload)


def list_findings(
    limit: int = 50,
    since: str | None = None,
    db_path: Path | str = DB_PATH,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM findings"
    params: list[Any] = []
    if since:
        query += " WHERE created_at >= ?"
        params.append(since)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["entity_ids"] = json.loads(item["entity_ids"]) if item.get("entity_ids") else []
        out.append(item)
    return out


def count_findings(db_path: Path | str = DB_PATH) -> int:
    with connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()
        return int(row["n"]) if row else 0
