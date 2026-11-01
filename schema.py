from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import psycopg
from psycopg import sql

from common import FatalError

KINDS = ("TEXT", "NUMERIC", "BIGINT", "DATE", "TIMESTAMP", "BOOLEAN", "TIME", "INTERVAL")

# Numeric-looking columns whose LAST name token is one of these are identifiers
# (references, account numbers, phone numbers ...) and are kept as TEXT.
ID_TOKENS = {"id", "ref", "reference", "no", "num", "number", "code", "acct", "account",
             "phone", "tel", "mobile", "msisdn", "zip", "postal", "stan", "rrn", "pan",
             "bin", "tin", "bvn", "nuban", "iban", "sku", "uuid", "guid", "cheque"}

_PG_TO_KIND = {
    "text": "TEXT", "character varying": "TEXT", "character": "TEXT", "citext": "TEXT",
    "numeric": "NUMERIC", "double precision": "NUMERIC", "real": "NUMERIC",
    "integer": "BIGINT", "bigint": "BIGINT", "smallint": "BIGINT",
    "date": "DATE", "timestamp without time zone": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMP", "boolean": "BOOLEAN",
    "time without time zone": "TIME", "interval": "INTERVAL",
}


def looks_like_identifier(name: str) -> bool:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    tokens = [t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t]
    return bool(tokens) and tokens[-1] in ID_TOKENS


def infer_types(header: list[str], sample_rows, overrides: dict | None = None) -> list[str]:
    n = len(header)
    seen = [set() for _ in range(n)]
    has_time = [False] * n
    for row in sample_rows:
        for i in range(min(n, len(row))):
            v = row[i]
            if v is None or v == "":
                continue
            c = v.__class__
            seen[i].add(c)
            if c is datetime and (v.hour or v.minute or v.second or v.microsecond):
                has_time[i] = True
    kinds = []
    for i, name in enumerate(header):
        ts = seen[i]
        if i == 0 or not ts:
            k = "TEXT"                                   # id is always preserved as TEXT
        elif ts <= {bool}:
            k = "BOOLEAN"
        elif ts <= {int, float}:
            k = "TEXT" if looks_like_identifier(name) else "NUMERIC"
        elif ts <= {datetime, date}:
            k = "TIMESTAMP" if has_time[i] else "DATE"
        elif ts <= {time}:
            k = "TIME"
        elif ts <= {timedelta}:
            k = "INTERVAL"
        else:
            k = "TEXT"
        kinds.append(k)
    for col, k in (overrides or {}).items():
        k = str(k).upper()
        if k not in KINDS:
            raise FatalError(f"Schema override for '{col}': unsupported type '{k}' (use {KINDS}).")
        if col not in header:
            raise FatalError(f"Schema override refers to unknown column '{col}'.")
        kinds[header.index(col)] = k
    return kinds


def table_exists(conn, schema: str, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM information_schema.tables "
                       "WHERE table_schema = %s AND table_name = %s", (schema, table)).fetchone()
    return row is not None


def create_table(conn, schema: str, table: str, header: list[str], kinds: list[str]) -> None:
    cols = [sql.SQL("{} {}{}").format(sql.Identifier(h), sql.SQL(k),
                                      sql.SQL(" NOT NULL" if i == 0 else ""))
            for i, (h, k) in enumerate(zip(header, kinds))]
    stmt = sql.SQL("CREATE TABLE {tbl} ({cols}, source_sheet TEXT NOT NULL, "
                   "imported_at TIMESTAMPTZ NOT NULL, PRIMARY KEY ({id}))").format(
        tbl=sql.Identifier(schema, table), cols=sql.SQL(", ").join(cols),
        id=sql.Identifier(header[0]))
    if schema != "public":
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    conn.execute(stmt)  # plain CREATE (never IF NOT EXISTS / DROP): cannot clobber a table


def verify_existing_table(conn, schema: str, table: str, header: list[str]) -> list[str]:
    """Check an existing destination table is compatible; return column kinds from it."""
    rows = conn.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position", (schema, table)).fetchall()
    cols = {r[0]: r for r in rows}
    missing = [h for h in header + ["source_sheet", "imported_at"] if h not in cols]
    if missing:
        raise FatalError(f"Existing table {schema}.{table} is missing columns: {missing}")
    for name, _, nullable, default in rows:
        if name not in header and name not in ("source_sheet", "imported_at") \
                and nullable == "NO" and default is None:
            raise FatalError(f"Existing table has extra NOT NULL column '{name}' without default.")
    qualified = sql.Identifier(schema, table).as_string(conn)
    ok = conn.execute(
        "SELECT 1 FROM pg_index i WHERE i.indrelid = %s::regclass AND i.indisunique "
        "AND i.indpred IS NULL AND i.indnatts = 1 AND i.indkey[0] = "
        "(SELECT attnum FROM pg_attribute WHERE attrelid = i.indrelid AND attname = %s)",
        (qualified, header[0])).fetchone()
    if not ok:
        raise FatalError(f"Column '{header[0]}' of {schema}.{table} has no PRIMARY KEY/UNIQUE "
                         f"constraint; refusing to import (duplicates could not be prevented).")
    kinds = []
    for h in header:
        k = _PG_TO_KIND.get(cols[h][1])
        if k is None:
            raise FatalError(f"Column '{h}' has unsupported PostgreSQL type '{cols[h][1]}'.")
        kinds.append(k)
    return kinds
