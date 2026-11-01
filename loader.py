from __future__ import annotations

import time

import psycopg
from psycopg import sql

from checkpoint import Meta
from common import FatalError
from db import connect
from validator import issue_text

MAX_RETRIES = 5
DB_DATA_ERRORS = (psycopg.errors.DataError, psycopg.errors.IntegrityError)


def short_error(e: Exception) -> str:
    text = str(e).strip()
    return (text.splitlines()[0] if text else e.__class__.__name__)[:500]


class BatchLoader:
    """COPY -> temp stage table -> INSERT .. ON CONFLICT DO NOTHING -> checkpoint update, all
    in ONE transaction per batch.

    * Connection loss: reconnect and retry (the checkpoint guard makes a retry after an
      unacknowledged commit harmless).
    * Database data errors (value too large for an existing column, constraint violation ...):
      the batch is re-run in the same transaction, bisecting with savepoints; only the rows that
      really fail are written to the '<table>_issues' table, every other row is loaded.
    * Every issue row (rejected / duplicate / database error) is stored in the issues table
      with its values as TEXT, plus sheet, Excel row number, job id, kind and reason."""

    def __init__(self, db_cfg, meta: Meta, job: dict, header, kinds, log, pk_name: str,
                 issues_name: str, include_dups: bool = True, isolate: bool = True):
        self.cfg, self.meta, self.log = db_cfg, meta, log
        self.job_id, self.job_ts = job["job_id"], job["imported_at"]
        self.include_dups, self.isolate_enabled = include_dups, isolate
        ident = sql.Identifier
        cols = sql.SQL(", ").join(ident(h) for h in header)
        idc = ident(pk_name)
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
        # after INSERT, in the top-level transaction: stage rows that conflicted
        self.q_dups = sql.SQL(
            "SELECT t._rn, (d.imported_at = %s::timestamptz) FROM _stage t "
            "JOIN {dest} d ON d.{id} = t.{id} "
            "WHERE d.xmin::text <> (txid_current() %% 4294967296)::text ORDER BY t._rn"
        ).format(dest=dest, id=idc)
        # before INSERT (used inside savepoints, where xmin is a sub-transaction id)
        self.q_dups_pre = sql.SQL(
            "SELECT t._rn, (d.imported_at = %s::timestamptz) FROM _stage t "
            "JOIN {dest} d ON d.{id} = t.{id} ORDER BY t._rn").format(dest=dest, id=idc)
        self.q_issue_copy = sql.SQL(
            "COPY {t} ({cols}, source_sheet, imported_at, _source_row, _job_id, _issue_kind, "
            "_issue_reason) FROM STDIN").format(t=ident(db_cfg.schema, issues_name), cols=cols)
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

    # ---------------------------------------------------------------- commit
    def commit(self, sheet, prev_last, last_row, rows, issues, read_n, blank_n,
               rejected_n, dup_batch) -> dict | None:
        args = (sheet, prev_last, last_row, rows, issues, read_n, blank_n, rejected_n, dup_batch)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                try:
                    return self._txn(*args, isolate=False)
                except DB_DATA_ERRORS as e:
                    if not self.isolate_enabled:
                        raise
                    self.log.warning("Rows %d-%d of '%s' hit a database error (%s); isolating "
                                     "the bad rows into the issues table", prev_last + 1,
                                     last_row, sheet, short_error(e))
                    return self._txn(*args, isolate=True)
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

    def _txn(self, sheet, prev_last, last_row, rows, issues, read_n, blank_n, rejected_n,
             dup_batch, isolate: bool) -> dict | None:
        issues = list(issues)              # a retry must not duplicate issue rows
        st = {"inserted": 0, "dup_existing": 0, "dup_cross": 0, "failed": 0}
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
                if isolate:
                    self._isolate(cur, sheet, rows, issues, st)
                else:
                    self.sheet_for_insert = sheet
                    ins, dex, dcr, new = self._insert_chunk(cur, rows, pre_join=False)
                    st["inserted"] += ins
                    st["dup_existing"] += dex
                    st["dup_cross"] += dcr
                    issues.extend(new)
            if issues:
                with cur.copy(self.q_issue_copy) as cp:
                    for rn, kind, reason, vals in issues:
                        cp.write_row(list(vals) + [sheet, self.job_ts, rn, self.job_id,
                                                   kind, reason])
            cur.execute(
                f"UPDATE {self.meta.sheets} SET last_row = %s, rows_read = rows_read + %s, "
                f"inserted = inserted + %s, dup_existing = dup_existing + %s, "
                f"dup_workbook = dup_workbook + %s, rejected = rejected + %s, "
                f"blank = blank + %s, status = 'running' WHERE job_id = %s AND sheet_name = %s",
                (last_row, read_n, st["inserted"], st["dup_existing"],
                 dup_batch + st["dup_cross"], rejected_n + st["failed"], blank_n,
                 self.job_id, sheet))
        return {"inserted": st["inserted"], "dup_existing": st["dup_existing"],
                "dup_workbook": dup_batch + st["dup_cross"], "failed": st["failed"]}

    # ------------------------------------------------------------- loading
    def _texts(self, vals) -> list:
        return [issue_text(v) for v in vals]

    def _insert_chunk(self, cur, rows, pre_join: bool):
        """COPY rows into the stage table and insert them. Returns
        (inserted, dup_existing, dup_in_job, issue_rows)."""
        with cur.copy(self.q_copy) as cp:
            for r in rows:
                cp.write_row(r)
        pre = cur.execute(self.q_dups_pre, (self.job_ts,)).fetchall() if pre_join else None
        cur.execute(self.q_insert, (self.sheet_for_insert, self.job_ts))
        inserted = cur.rowcount
        conflicts = len(rows) - inserted
        dex = dcr = 0
        out: list = []
        if conflicts:
            found = pre if pre_join else cur.execute(self.q_dups, (self.job_ts,)).fetchall()
            by_rn = {r[-1]: r for r in rows} if self.include_dups else None
            for rn, in_job in found:
                if in_job:
                    dcr += 1
                else:
                    dex += 1
                if by_rn is not None:
                    out.append((rn, "DUPLICATE_IN_WORKBOOK" if in_job else "DUPLICATE_EXISTING",
                                "primary key already imported earlier in this job" if in_job
                                else "primary key already exists in the destination table",
                                self._texts(by_rn[rn][:-1])))
            if len(found) < conflicts:        # concurrent insert raced us; cannot identify rows
                dcr += conflicts - len(found)
        return inserted, dex, dcr, out

    def _isolate(self, cur, sheet, rows, issues, st) -> None:
        """The whole batch is known to fail: split it, load what loads, report what does not."""
        self.sheet_for_insert = sheet
        if len(rows) == 1:
            self._try(cur, sheet, rows, issues, st)
            return
        mid = len(rows) // 2
        for part in (rows[:mid], rows[mid:]):
            self._try(cur, sheet, part, issues, st)

    def _try(self, cur, sheet, rows, issues, st) -> None:
        try:
            with self.conn.transaction():                  # savepoint
                ins, dex, dcr, new = self._insert_chunk(cur, rows, pre_join=True)
                cur.execute("DELETE FROM _stage")
        except DB_DATA_ERRORS as e:
            if len(rows) == 1:
                issues.append((rows[0][-1], "DB_ERROR", short_error(e), self._texts(rows[0][:-1])))
                st["failed"] += 1
            else:
                self._isolate(cur, sheet, rows, issues, st)
            return
        st["inserted"] += ins
        st["dup_existing"] += dex
        st["dup_cross"] += dcr
        issues.extend(new)

    sheet_for_insert = None
