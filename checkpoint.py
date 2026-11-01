from __future__ import annotations

import json
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from common import FatalError
from config import CHECKPOINT_DIR
from db import qname


class Meta:
    """Quoted names of the bookkeeping tables (they live in the destination schema)."""

    def __init__(self, conn, schema: str):
        self.schema = schema
        self.jobs = qname(conn, schema, "import_jobs")
        self.sheets = qname(conn, schema, "import_job_sheets")


def ensure_meta(conn, meta: Meta) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {meta.jobs} (
        job_id TEXT PRIMARY KEY, file_path TEXT, file_name TEXT, file_size BIGINT,
        file_mtime DOUBLE PRECISION, fingerprint TEXT, db_name TEXT, schema_name TEXT,
        table_name TEXT, batch_size INT, status TEXT, header JSONB, col_types JSONB,
        imported_at TIMESTAMPTZ, started_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ, error TEXT, settings JSONB)""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {meta.sheets} (
        job_id TEXT NOT NULL, sheet_order INT NOT NULL, sheet_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', total_rows BIGINT,
        last_row BIGINT NOT NULL DEFAULT 0, rows_read BIGINT NOT NULL DEFAULT 0,
        inserted BIGINT NOT NULL DEFAULT 0, dup_existing BIGINT NOT NULL DEFAULT 0,
        dup_workbook BIGINT NOT NULL DEFAULT 0, rejected BIGINT NOT NULL DEFAULT 0,
        blank BIGINT NOT NULL DEFAULT 0, error TEXT, finished_at TIMESTAMPTZ,
        PRIMARY KEY (job_id, sheet_name))""")


def now() -> datetime:
    return datetime.now(timezone.utc)


def next_job_id(conn, meta: Meta) -> str:
    prefix = f"import_{datetime.now():%Y%m%d}_"
    n = conn.execute(f"SELECT count(*) FROM {meta.jobs} WHERE job_id LIKE %s",
                     (prefix + "%",)).fetchone()[0]
    return f"{prefix}{n + 1:03d}"


def create_job(conn, meta: Meta, job: dict, sheet_names: list[str]) -> None:
    conn.execute(
        f"INSERT INTO {meta.jobs} (job_id, file_path, file_name, file_size, file_mtime, "
        f"fingerprint, db_name, schema_name, table_name, batch_size, status, imported_at, "
        f"started_at, updated_at, settings) VALUES (%(job_id)s, %(file_path)s, %(file_name)s, "
        f"%(file_size)s, %(file_mtime)s, %(fingerprint)s, %(db_name)s, %(schema_name)s, "
        f"%(table_name)s, %(batch_size)s, 'running', %(imported_at)s, %(started_at)s, "
        f"%(started_at)s, %(settings)s)", {**job, "settings": Jsonb(job["settings"])})
    for i, name in enumerate(sheet_names):
        conn.execute(f"INSERT INTO {meta.sheets} (job_id, sheet_order, sheet_name) "
                     f"VALUES (%s, %s, %s)", (job["job_id"], i, name))


def get_job(conn, meta: Meta, job_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(f"SELECT * FROM {meta.jobs} WHERE job_id = %s", (job_id,)).fetchone()


def get_sheets(conn, meta: Meta, job_id: str) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(f"SELECT * FROM {meta.sheets} WHERE job_id = %s ORDER BY sheet_order",
                           (job_id,)).fetchall()


def list_jobs(conn, meta: Meta, limit: int = 50) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"SELECT j.job_id, j.file_name, j.table_name, j.status, j.started_at, j.updated_at, "
            f"coalesce(sum(s.inserted),0) AS inserted, coalesce(sum(s.rejected),0) AS rejected "
            f"FROM {meta.jobs} j LEFT JOIN {meta.sheets} s USING (job_id) "
            f"GROUP BY j.job_id ORDER BY j.started_at DESC LIMIT %s", (limit,)).fetchall()


def set_job_status(conn, meta: Meta, job_id: str, status: str, error: str | None = None,
                   finished: bool = False) -> None:
    conn.execute(f"UPDATE {meta.jobs} SET status = %s, error = %s, updated_at = now(), "
                 f"finished_at = CASE WHEN %s THEN now() ELSE finished_at END WHERE job_id = %s",
                 (status, error, finished, job_id))


def touch_job(conn, meta: Meta, job_id: str) -> None:
    conn.execute(f"UPDATE {meta.jobs} SET updated_at = now() WHERE job_id = %s", (job_id,))


def set_job_schema(conn, meta: Meta, job_id: str, header: list[str], info: dict) -> None:
    """header = full reference header (validation). info = table layout:
    selected, table_cols, kinds, pk, pk_name, sep, issues_table."""
    conn.execute(f"UPDATE {meta.jobs} SET header = %s, col_types = %s WHERE job_id = %s",
                 (Jsonb(header), Jsonb(info), job_id))


def set_sheet_status(conn, meta: Meta, job_id: str, sheet: str, status: str,
                     error: str | None = None) -> None:
    conn.execute(f"UPDATE {meta.sheets} SET status = %s, error = %s, "
                 f"finished_at = CASE WHEN %s IN ('completed','failed') THEN now() "
                 f"ELSE finished_at END WHERE job_id = %s AND sheet_name = %s",
                 (status, error, status, job_id, sheet))


def write_local_checkpoint(job: dict, sheets: list[dict]) -> None:
    """Secondary recovery aid: a JSON mirror of the DB checkpoint."""
    try:
        data = {"job": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                        for k, v in job.items()},
                "sheets": [{k: (v.isoformat() if hasattr(v, "isoformat") else v)
                            for k, v in s.items()} for s in sheets]}
        (CHECKPOINT_DIR / f"{job['job_id']}.json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass
