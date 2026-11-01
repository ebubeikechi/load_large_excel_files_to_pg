from __future__ import annotations

import time

import psycopg
from psycopg import sql

from checkpoint import Meta
from common import FatalError
from db import connect

MAX_RETRIES = 5


class BatchLoader:
    """COPY -> temp stage table -> INSERT .. ON CONFLICT DO NOTHING -> checkpoint update,
    all in ONE transaction per batch. Reconnects and retries on connection loss; the
    checkpoint guard makes a retry after an unacknowledged commit harmless."""

    def __init__(self, db_cfg, meta: Meta, job: dict, header, kinds, log, issue_budget: int,
                 pk_name: str | None = None):
        self.cfg, self.meta, self.log = db_cfg, meta, log
        self.job_id, self.job_ts = job["job_id"], job["imported_at"]
        self.budget = issue_budget
        ident = sql.Identifier
        cols = sql.SQL(", ").join(ident(h) for h in header)
        idc = ident(pk_name or header[0])
        dest = ident(db_cfg.schema, job["table_name"])
        self.q_stage = sql.SQL("CREATE TEMP TABLE IF NOT EXISTS _stage ({defs}, _rn BIGINT) "
                               "ON COMMIT DELETE ROWS").format(
            defs=sql.SQL(", ").join(sql.SQL("{} {}").format(ident(h), sql.SQL(k))
                                    for h, k in zip(header, kinds)))
        self.q_copy = sql.SQL("COPY _stage ({cols}, _rn) FROM STDIN").format(cols=cols)
        self.q_insert = sql.SQL(
            "INSERT INTO {dest} ({cols}, source_sheet, imported_at) "
            "SELECT {cols}, %s::text, %s::timestamptz FROM _stage ORDER BY {id} "
            "ON CONFLICT ({id}) DO NOTHING").format(dest=dest, cols=cols, id=idc)
        self.q_dups = sql.SQL(
            "SELECT t._rn, t.{id}::text, (d.imported_at = %s::timestamptz) "
            "FROM _stage t JOIN {dest} d ON d.{id} = t.{id} "
            "WHERE d.xmin::text <> (txid_current() %% 4294967296)::text ORDER BY t._rn"
        ).format(dest=dest, id=idc)
        self.conn = None
        self._connect()

    # -------------------------------------------------------------- plumbing
    def _connect(self) -> None:
        self.conn = connect(self.cfg)
        self.conn.execute(self.q_stage)

    def _reconnect(self, attempt: int) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        last = None
        for k in range(1, 6):
            time.sleep(min(30, 2 * attempt * k))
            try:
                self._connect()
                self.log.warning("Reconnected to PostgreSQL")
                return
            except psycopg.Error as e:
                last = e
                self.log.error("Reconnect attempt %d failed: %s", k, e)
        raise FatalError(f"Lost connection to PostgreSQL and could not reconnect: {last}")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def take_budget(self) -> bool:
        if self.budget > 0:
            self.budget -= 1
            return True
        return False

    # ---------------------------------------------------------------- commit
    def commit(self, sheet, prev_last, last_row, rows, issues, read_n, blank_n,
               rejected_n, dup_batch) -> dict | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._commit_once(sheet, prev_last, last_row, rows, issues,
                                         read_n, blank_n, rejected_n, dup_batch)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                self.log.error("Connection problem committing rows %d-%d (attempt %d/%d): %s",
                               prev_last + 1, last_row, attempt, MAX_RETRIES, e)
                if attempt == MAX_RETRIES:
                    raise FatalError(f"Database connection lost: {e}") from None
                self._reconnect(attempt)
            except psycopg.Error as e:
                self.log.error("ROLLBACK rows %d-%d of '%s': %s", prev_last + 1, last_row, sheet, e)
                raise FatalError(f"Batch rows {prev_last + 1}-{last_row} of '{sheet}' failed "
                                 f"and was rolled back: {e}") from None

    def _commit_once(self, sheet, prev_last, last_row, rows, issues, read_n, blank_n,
                     rejected_n, dup_batch) -> dict | None:
        issues = list(issues)          # retries must not duplicate detail rows
        inserted = dup_existing = dup_cross = 0
        with self.conn.transaction():
            cur = self.conn.cursor()
            db_last = cur.execute(
                f"SELECT last_row FROM {self.meta.sheets} WHERE job_id = %s AND sheet_name = %s",
                (self.job_id, sheet)).fetchone()[0]
            if db_last != prev_last:
                if db_last == last_row:      # commit succeeded earlier, ack was lost
                    self.log.warning("Batch ending at row %d was already committed", last_row)
                    return None
                raise FatalError(f"Checkpoint mismatch for '{sheet}': expected {prev_last}, "
                                 f"database has {db_last}")
            if rows:
                with cur.copy(self.q_copy) as cp:
                    for r in rows:
                        cp.write_row(r)
                cur.execute(self.q_insert, (sheet, self.job_ts))
                inserted = cur.rowcount
                conflicts = len(rows) - inserted
                if conflicts:
                    found = cur.execute(self.q_dups, (self.job_ts,)).fetchall()
                    for rn, rid, in_job in found:
                        if in_job:
                            dup_cross += 1
                        else:
                            dup_existing += 1
                        if self.take_budget():
                            issues.append((self.job_id, sheet, rn, rid, "DUPLICATE",
                                           "id already imported earlier in this job" if in_job
                                           else "id already exists in destination table"))
            if issues:
                with cur.copy(f"COPY {self.meta.issues} (job_id, sheet_name, row_num, row_id, "
                              f"kind, reason) FROM STDIN") as cp:
                    for it in issues:
                        cp.write_row(it)
            cur.execute(
                f"UPDATE {self.meta.sheets} SET last_row = %s, rows_read = rows_read + %s, "
                f"inserted = inserted + %s, dup_existing = dup_existing + %s, "
                f"dup_workbook = dup_workbook + %s, rejected = rejected + %s, "
                f"blank = blank + %s, status = 'running' WHERE job_id = %s AND sheet_name = %s",
                (last_row, read_n, inserted, dup_existing, dup_batch + dup_cross, rejected_n,
                 blank_n, self.job_id, sheet))
        return {"inserted": inserted, "dup_existing": dup_existing,
                "dup_workbook": dup_batch + dup_cross}
