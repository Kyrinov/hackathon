from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

_conn: psycopg.Connection | None = None


def _new_conn() -> psycopg.Connection:
    load_dotenv()
    conninfo = os.getenv("DATABASE_URL") or os.getenv("DB_CONNECTION_STRING")
    if conninfo:
        return psycopg.connect(
            conninfo,
            sslmode="require",
            autocommit=True,
            row_factory=dict_row,
        )

    required_vars = ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE")
    if not all(os.getenv(var) for var in required_vars):
        raise RuntimeError(
            "DATABASE_URL not set; load event-day .env (see README) "
            "or run with `use_demo=True` for the synthetic demo dataset."
        )

    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=int(os.getenv("PGPORT", "5432")),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"],
        sslmode="require",
        autocommit=True,
        row_factory=dict_row,
    )


def get_conn() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = _new_conn()
    return _conn
