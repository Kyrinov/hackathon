#!/usr/bin/env python3
"""OFFLINE-ONLY: organizers ran Splink for this hackathon. Do not run
on demo day — the entity resolution step takes ~45 min and writes to
the shared DB. Kept for reproducibility only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents import state  # noqa: E402
from src.db.connection import get_conn  # noqa: E402
from src.db.queries import _status_where  # noqa: E402

if TYPE_CHECKING:
    import pandas as pd

try:
    from cleanco import basename
except ImportError:

    def basename(name: str) -> str:
        return name


_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:INCORPORATED|INC|LTD|LIMITED|CORP|CORPORATION|CO|COMPANY|SOCIETY|FOUNDATION)\.?\s*$",
    re.IGNORECASE,
)


_NOISE_SUFFIX_RE = re.compile(
    r"\s*\b(?:"
    r"ASSUMED(?:\s+NAME(?:\s+.*)?)?"
    r"|AKA(?:\s+.*)?"
    r"|O\s*/\s*A(?:\s+.*)?"
    r"|D\s*/\s*B\s*/\s*A(?:\s+.*)?"
    r"|DBA(?:\s+.*)?"
    r"|DOING\s+BUSINESS\s+AS(?:\s+.*)?"
    r"|OPERATING\s+AS(?:\s+.*)?"
    r"|TRADING\s+AS(?:\s+.*)?"
    r"|TRADE\s+NAME\s+OF(?:\s+.*)?"
    r"|FORMERLY(?:\s+.*)?"
    r"|F\s*/\s*K\s*/\s*A(?:\s+.*)?"
    r")$",
    re.IGNORECASE,
)
_PLACEHOLDER_BNS = {
    "000000000",
    "100000000",
    "200000000",
    "300000000",
    "400000000",
    "500000000",
    "600000000",
    "700000000",
    "800000000",
    "900000000",
    "320000000",
}


def log(message: str) -> None:
    print(f"[splink-resolution] {message}", flush=True)


def clean_name(name: Any) -> str:
    if not name:
        return ""
    s = str(name)
    for sep in [" │ ", " | ", "│", " / "]:
        if sep in s:
            parts = [part.strip() for part in s.split(sep)]
            if len(parts) >= 2:
                s = parts[0]
                break
    s = _NOISE_SUFFIX_RE.sub("", s).strip()
    cleaned = basename(s) or s
    prior = ""
    while cleaned and cleaned != prior:
        prior = cleaned
        cleaned = _LEGAL_SUFFIX_RE.sub("", cleaned).strip(" ,.-")
    out = re.sub(r"\s+", " ", cleaned.strip().upper())
    return out


def bn_root(value: Any) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 9:
        return None
    root = digits[:9]
    if root in _PLACEHOLDER_BNS or root.endswith("00000000"):
        return None
    return root


def _stable_id(*parts: Any) -> str:
    raw = "::".join(str(part) for part in parts if part not in (None, ""))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _address_field(addresses: Any, *keys: str) -> str | None:
    if isinstance(addresses, str):
        try:
            addresses = json.loads(addresses)
        except json.JSONDecodeError:
            return None
    candidates = addresses if isinstance(addresses, list) else [addresses]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        lowered = {str(key).lower(): value for key, value in item.items()}
        for key in keys:
            value = lowered.get(key.lower())
            if value:
                return str(value).strip().upper()
    return None


def _alias_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name") or value.get("alias") or value.get("legal_name")
    return None


def extract_records(limit: int | None = None) -> pd.DataFrame:
    import pandas as pd

    conn = get_conn()
    query = f"""
        SELECT
            e.id AS entity_id,
            e.canonical_name,
            e.bn_root,
            e.entity_type,
            COALESCE(e.aliases, '[]'::jsonb) AS aliases,
            COALESCE(e.dataset_sources, ARRAY[]::text[]) AS dataset_sources,
            COALESCE(e.source_link_count, 0)::int AS source_link_count,
            COALESCE(e.addresses, '[]'::jsonb) AS addresses
        FROM general.entity_golden_records e
        WHERE {_status_where('e')}
        ORDER BY e.id
        LIMIT COALESCE(%(limit)s, 1000000000)
    """
    link_query = f"""
        SELECT
            e.id AS entity_id,
            esl.id AS source_link_id,
            esl.source_name,
            esl.source_schema,
            esl.source_table
        FROM general.entity_source_links esl
        JOIN general.entity_golden_records e ON e.id = esl.entity_id
        WHERE {_status_where('e')}
          AND esl.source_name IS NOT NULL
        ORDER BY esl.id
        LIMIT COALESCE(%(limit)s, 1000000000)
    """
    rows: list[dict[str, Any]] = []
    entity_meta: dict[int, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(query, {"limit": limit})
        for row in cur.fetchall():
            entity_id = int(row["entity_id"])
            city = _address_field(row.get("addresses"), "city", "municipality")
            province = _address_field(row.get("addresses"), "province", "province_code", "state")
            postal_code = _address_field(row.get("addresses"), "postal_code", "postal", "zip")
            datasets = row.get("dataset_sources") or []
            source_dataset = ",".join(str(item) for item in datasets) or "golden"
            meta = {
                "entity_id": entity_id,
                "bn_root": bn_root(row.get("bn_root")),
                "city": city,
                "province": province,
                "postal_code": postal_code.replace(" ", "") if postal_code else None,
                "entity_type": row.get("entity_type"),
                "source_count": int(row.get("source_link_count") or 0),
            }
            entity_meta[entity_id] = meta
            rows.append(
                _record(
                    record_id=f"entity:{entity_id}:canonical",
                    source_dataset=source_dataset,
                    legal_name=row.get("canonical_name"),
                    **meta,
                )
            )
            for alias in row.get("aliases") or []:
                alias_text = _alias_name(alias)
                if alias_text:
                    rows.append(
                        _record(
                            record_id=f"entity:{entity_id}:alias:{_stable_id(alias_text)}",
                            source_dataset=source_dataset,
                            legal_name=alias_text,
                            **meta,
                        )
                    )
        cur.execute(link_query, {"limit": limit})
        for row in cur.fetchall():
            entity_id = int(row["entity_id"])
            meta = entity_meta.get(entity_id)
            if not meta:
                continue
            source_dataset = f"{row.get('source_schema')}.{row.get('source_table')}"
            rows.append(
                _record(
                    record_id=f"source_link:{row['source_link_id']}",
                    source_dataset=source_dataset,
                    legal_name=row.get("source_name"),
                    **meta,
                )
            )
    df = pd.DataFrame(row for row in rows if row.get("legal_name") and row.get("cleaned_name"))
    if df.empty:
        return df
    return df.drop_duplicates(
        subset=["entity_id", "cleaned_name", "bn_root", "city", "province", "source_dataset"],
        keep="first",
    ).reset_index(drop=True)


def _record(
    record_id: str,
    source_dataset: str,
    legal_name: Any,
    entity_id: int,
    bn_root: str | None,
    city: str | None,
    province: str | None,
    postal_code: str | None,
    entity_type: str | None,
    source_count: int,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "entity_id": entity_id,
        "legal_name": str(legal_name or "").strip(),
        "cleaned_name": clean_name(legal_name),
        "bn_root": bn_root,
        "city": city,
        "province": province,
        "postal_code": postal_code,
        "entity_type": entity_type,
        "source_dataset": source_dataset,
        "source_count": source_count,
    }


def configure_splink() -> Any:
    from splink import SettingsCreator, block_on
    import splink.comparison_library as cl

    return SettingsCreator(
        link_type="link_and_dedupe",
        unique_id_column_name="record_id",
        probability_two_random_records_match=1 / 50_000,
        comparisons=[
            cl.ExactMatch("bn_root").configure(term_frequency_adjustments=True),
            cl.JaroWinklerAtThresholds("cleaned_name", [0.92, 0.82]).configure(
                term_frequency_adjustments=True
            ),
            cl.ExactMatch("city"),
            cl.ExactMatch("province"),
            cl.ExactMatch("entity_type"),
        ],
        blocking_rules_to_generate_predictions=[
            block_on("bn_root"),
            block_on("cleaned_name"),
            block_on("province", 'substr("cleaned_name", 1, 5)'),
            block_on("city", 'substr("cleaned_name", 1, 5)'),
        ],
        retain_matching_columns=True,
        retain_intermediate_calculation_columns=False,
    )


def run_splink(records_df: pd.DataFrame, threshold: float, memory_limit: str) -> tuple[pd.DataFrame, int, str]:
    import duckdb
    import splink
    from splink import DuckDBAPI, Linker, block_on

    duck = duckdb.connect(":memory:")
    duck.execute(f"SET memory_limit='{memory_limit}'")
    duck.execute("SET threads=4")
    duck.execute("SET preserve_insertion_order=false")
    tmp = Path("data/duckdb_tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    duck.execute("SET temp_directory=?", [str(tmp)])
    linker = Linker(records_df, configure_splink(), db_api=DuckDBAPI(connection=duck))

    log("Estimating u probabilities from random samples")
    linker.training.estimate_u_using_random_sampling(max_pairs=1_000_000)
    for block in (block_on("bn_root"), block_on("cleaned_name")):
        try:
            linker.training.estimate_parameters_using_expectation_maximisation(
                block,
                fix_u_probabilities=False,
            )
        except Exception as exc:
            log(f"EM training skipped for block {block}: {exc}")

    predictions = linker.inference.predict(threshold_match_probability=threshold)
    predictions_df = predictions.as_pandas_dataframe()
    clusters = linker.clustering.cluster_pairwise_predictions_at_threshold(
        predictions,
        threshold_match_probability=threshold,
    )
    clusters_df = clusters.as_pandas_dataframe()
    cluster_count = int(clusters_df["cluster_id"].nunique()) if "cluster_id" in clusters_df else 0
    return predictions_df, cluster_count, splink.__version__


def build_candidates(records_df: pd.DataFrame, predictions_df: pd.DataFrame) -> list[dict[str, Any]]:
    by_record = records_df.set_index("record_id").to_dict(orient="index")
    best_by_entity_pair: dict[tuple[int, int], dict[str, Any]] = {}
    for _, row in predictions_df.iterrows():
        left_id = row.get("record_id_l") or row.get("unique_id_l")
        right_id = row.get("record_id_r") or row.get("unique_id_r")
        if left_id not in by_record or right_id not in by_record:
            continue
        left = by_record[left_id]
        right = by_record[right_id]
        entity_l = int(left["entity_id"])
        entity_r = int(right["entity_id"])
        if entity_l == entity_r:
            continue
        key = tuple(sorted((entity_l, entity_r)))
        probability = float(row.get("match_probability") or 0.0)
        candidate = {
            "entity_id_l": entity_l,
            "entity_id_r": entity_r,
            "record_id_l": left_id,
            "record_id_r": right_id,
            "legal_name_l": left.get("legal_name"),
            "legal_name_r": right.get("legal_name"),
            "cleaned_name_l": left.get("cleaned_name"),
            "cleaned_name_r": right.get("cleaned_name"),
            "bn_root_l": left.get("bn_root"),
            "bn_root_r": right.get("bn_root"),
            "city_l": left.get("city"),
            "city_r": right.get("city"),
            "province_l": left.get("province"),
            "province_r": right.get("province"),
            "source_dataset_l": left.get("source_dataset"),
            "source_dataset_r": right.get("source_dataset"),
            "source_count_l": int(left.get("source_count") or 0),
            "source_count_r": int(right.get("source_count") or 0),
            "match_probability": probability,
            "match_weight": float(row.get("match_weight") or 0.0),
            "method": "splink_link_and_dedupe",
        }
        prior = best_by_entity_pair.get(key)
        if prior is None or probability > float(prior["match_probability"]):
            best_by_entity_pair[key] = candidate
    return list(best_by_entity_pair.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Splink entity resolution into SQLite.")
    parser.add_argument("--threshold", type=float, default=0.40)
    parser.add_argument("--limit", type=int, default=None, help="Limit golden records and source links for dry runs.")
    parser.add_argument("--memory-limit", default="10GB")
    parser.add_argument("--db-path", default=str(state.DB_PATH))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start = time.time()
    db_path = Path(args.db_path)
    state.init_db(db_path)
    run_id: int | None = None
    try:
        log("Extracting active golden records and source-link names")
        records_df = extract_records(limit=args.limit)
        if records_df.empty:
            log("No records extracted; nothing to run")
            return 0
        log(f"Prepared {len(records_df):,} Splink input records")
        predictions_df, cluster_count, splink_version = run_splink(
            records_df,
            args.threshold,
            args.memory_limit,
        )
        run_id = state.start_splink_run(
            threshold=args.threshold,
            splink_version=splink_version,
            config={"limit": args.limit, "memory_limit": args.memory_limit},
            db_path=db_path,
        )
        candidates = build_candidates(records_df, predictions_df)
        inserted = state.insert_splink_candidates(run_id, candidates, db_path=db_path)
        state.finish_splink_run(
            run_id,
            record_count=len(records_df),
            candidate_count=inserted,
            cluster_count=cluster_count,
            db_path=db_path,
        )
        log(f"Persisted {inserted:,} entity candidates in run {run_id}")
        log(f"Done in {time.time() - start:.0f}s")
        return 0
    except Exception as exc:
        if run_id is not None:
            state.finish_splink_run(run_id, 0, 0, status="failed", error=str(exc), db_path=db_path)
        raise


if __name__ == "__main__":
    sys.exit(main())
