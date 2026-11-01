from __future__ import annotations

import psycopg
from psycopg import sql

from common import FatalError
from config import DbConfig


def explain_error(e: Exception, cfg: DbConfig) -> str:
    msg = str(e).strip()
    low = msg.lower()
    if "password authentication failed" in low or "authentication failed" in low:
        return f"Authentication failed for user '{cfg.user}'. Check DB_USER / DB_PASSWORD."
    if "database" in low and "does not exist" in low:
        return (f"Database '{cfg.name}' does not exist. Create it first "
                f"(CREATE DATABASE {cfg.name};) or fix DB_NAME.")
    if "refused" in low or "could not connect" in low or "timeout" in low:
        return f"Cannot reach PostgreSQL at {cfg.host}:{cfg.port}. Is the server running?"
    return msg


def check_connection(cfg: DbConfig) -> str:
    """Returns the server version or raises FatalError with a friendly message."""
    try:
        with psycopg.connect(cfg.conninfo(), connect_timeout=8) as c:
            return c.execute("SHOW server_version").fetchone()[0]
    except psycopg.Error as e:
        raise FatalError(explain_error(e, cfg)) from None


def connect(cfg: DbConfig, tune: bool = True) -> psycopg.Connection:
    """Autocommit connection; real transactions are opened with `conn.transaction()`."""
    conn = psycopg.connect(cfg.conninfo(), autocommit=True, connect_timeout=10)
    if tune:
        import config
        if config.SYNCHRONOUS_COMMIT_OFF:              # safe: checkpoint commits with the data
            conn.execute("SET synchronous_commit = off")
        conn.execute("SET temp_buffers = '128MB'")
    return conn


def qname(conn, schema: str, name: str) -> str:
    return sql.Identifier(schema, name).as_string(conn)
