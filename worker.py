from __future__ import annotations

import time
from collections import deque
from itertools import chain, islice

from checkpoint import (Meta, get_job, get_sheets, set_job_schema, set_sheet_status)
from common import FatalError
from config import DbConfig, ImportSettings
from db import connect
from excel_reader import open_sheet
from loader import BatchLoader
from logger import get_logger
from schema import create_table, infer_types, table_exists, verify_existing_table
from validator import (EMPTY, RowConverter, RowError, compare_headers, normalize_header,
                       row_id_text, validate_header_names)

ABORT_JOB_STATES = ("failed", "interrupted")


def run_sheet(task: dict) -> dict:
    """Entry point executed in a worker process: imports ONE worksheet."""
    cfg = DbConfig(**task["db"])
    st = ImportSettings(**task["settings"])
    job_id, sheet = task["job_id"], task["sheet"]
    log = get_logger(job_id)
    conn = loader = None
    try:
        conn = connect(cfg)
        meta = Meta(conn, cfg.schema)
        return _run(task, cfg, st, conn, meta, log)
    except (FatalError, KeyboardInterrupt) as e:
        msg = str(e) or "interrupted"
        log.critical("Worksheet '%s' stopped: %s", sheet, msg)
        _mark_failed(cfg, task, msg)
        return {"sheet": sheet, "ok": False, "error": msg}
    except Exception as e:  # unexpected
        log.exception("Worksheet '%s' crashed", sheet)
        msg = f"{type(e).__name__}: {e}"
        _mark_failed(cfg, task, msg)
        return {"sheet": sheet, "ok": False, "error": msg}
    finally:
        if conn is not None:
            conn.close()


def _mark_failed(cfg, task, msg):
    try:
        with connect(cfg, tune=False) as c:
            set_sheet_status(c, Meta(c, cfg.schema), task["job_id"], task["sheet"], "failed", msg)
    except Exception:
        pass


def _resolve_selection(header: list[str], wanted) -> list[str]:
    """Selected column names in header order; the first column (primary key) is always kept."""
    if not wanted:
        return list(header)
    missing = [c for c in wanted if c not in header]
    if missing:
        raise FatalError(f"Selected column(s) not found in the worksheet header: {missing[:5]}")
    keep = set(wanted) | {header[0]}
    return [h for h in header if h in keep]


def _wait(conn, meta, job_id, predicate, sleep=0.5):
    while True:
        job = get_job(conn, meta, job_id)
        if job is None or job["status"] in ABORT_JOB_STATES:
            raise FatalError("Job was aborted.")
        sheets = get_sheets(conn, meta, job_id)
        bad = [s for s in sheets if s["status"] == "failed"]
        if bad:
            raise FatalError(f"Aborted because worksheet '{bad[0]['sheet_name']}' failed: "
                             f"{bad[0]['error']}")
        if predicate(job, sheets):
            return job
        time.sleep(sleep)


def _run(task, cfg, st, conn, meta, log) -> dict:
    job_id, sheet, order = task["job_id"], task["sheet"], task["order"]
    set_sheet_status(conn, meta, job_id, sheet, "loading")
    log.info("Worksheet '%s': parsing started (engine=%s)", sheet, st.engine)
    t0 = time.perf_counter()
    stream = open_sheet(task["file"], sheet, st.engine)
    conn.execute(f"UPDATE {meta.sheets} SET total_rows = %s WHERE job_id = %s AND sheet_name = %s",
                 (max(stream.height - 1, 0) if stream.height else None, job_id, sheet))
    log.info("Worksheet '%s': parsed in %.1fs (%d rows incl. header)",
             sheet, time.perf_counter() - t0, stream.height)

    header_row = next(stream.rows, None)
    if header_row is None:
        raise FatalError(f"Worksheet '{sheet}' is empty.")
    header = normalize_header(header_row)
    validate_header_names(header)

    job = get_job(conn, meta, job_id)
    sample = []
    if job["header"] is None:
        if order != 0:                        # wait for the reference (first) worksheet
            job = _wait(conn, meta, job_id, lambda j, s: j["header"] is not None)
        else:
            sample = list(islice(stream.rows, st.sample_rows))
            selected = _resolve_selection(header, st.selected_columns)
            pos = [header.index(c) for c in selected]
            sample_sel = [[r[i] if i < len(r) else None for i in pos] for r in sample]
            if table_exists(conn, cfg.schema, task["table"]):
                kinds = verify_existing_table(conn, cfg.schema, task["table"], selected)
                log.info("Existing table %s.%s verified (append mode)", cfg.schema, task["table"])
            else:
                kinds = infer_types(selected, sample_sel, st.schema_overrides)
                create_table(conn, cfg.schema, task["table"], selected, kinds)
                log.info("Created table %s.%s with types %s", cfg.schema, task["table"],
                         dict(zip(selected, kinds)))
            log.info("Importing %d of %d columns", len(selected), len(header))
            set_job_schema(conn, meta, job_id, header, selected, kinds)
            job = get_job(conn, meta, job_id)
    ref, info = job["header"], job["col_types"]
    if isinstance(info, list):                # jobs created by older versions
        selected, kinds = ref, info
    else:
        selected, kinds = info["selected"], info["kinds"]
    compare_headers(ref, header, sheet)
    log.info("Worksheet '%s': header validated (%d columns, %d imported)", sheet, len(header),
             len(selected))
    indices = None if selected == header else [header.index(c) for c in selected]

    set_sheet_status(conn, meta, job_id, sheet, "validated")
    barrier = task.get("barrier") or []
    if barrier:      # nothing is committed until EVERY worksheet header has been validated
        _wait(conn, meta, job_id, lambda j, ss: all(
            s["status"] in ("validated", "running", "completed")
            for s in ss if s["sheet_name"] in barrier))
    set_sheet_status(conn, meta, job_id, sheet, "running")

    me = next(s for s in get_sheets(conn, meta, job_id) if s["sheet_name"] == sheet)
    resume_last = me["last_row"]
    rows = chain(sample, stream.rows)
    if resume_last > stream.first_row:
        skip = resume_last - stream.first_row
        log.info("RECOVERY: '%s' resuming after Excel row %d (skipping %d rows)",
                 sheet, resume_last, skip)
        deque(islice(rows, skip), maxlen=0)
        start_row = resume_last + 1
    else:
        resume_last, start_row = 0, stream.first_row + 1

    used = conn.execute(f"SELECT count(*) FROM {meta.issues} WHERE job_id = %s AND sheet_name = %s",
                        (job_id, sheet)).fetchone()[0]
    loader = BatchLoader(cfg, meta, job, selected, kinds, log, max(st.max_issue_rows - used, 0))
    try:
        _import_rows(loader, task, st, RowConverter(selected, kinds, indices, len(header)), rows, start_row,
                     resume_last, me["rejected"], log)
    finally:
        loader.close()
        stream.close()
    set_sheet_status(conn, meta, job_id, sheet, "completed")
    log.info("Worksheet '%s': COMPLETED", sheet)
    return {"sheet": sheet, "ok": True}


def _import_rows(loader, task, st, conv, rows, start_row, prev_last, rejected_total, log):
    job_id, sheet, batch_size = task["job_id"], task["sheet"], st.batch_size
    convert = conv.convert
    good, issues, seen = [], [], {}
    n = blank = rejected = dup_batch = 0
    row_num = start_row - 1
    t_batch = time.perf_counter()

    def flush():
        nonlocal good, issues, seen, n, blank, rejected, dup_batch, prev_last
        nonlocal rejected_total, t_batch
        res = loader.commit(sheet, prev_last, row_num, good, issues, n - blank, blank,
                            rejected, dup_batch)
        dt = time.perf_counter() - t_batch
        if res is not None:
            log.info("COMMIT '%s' rows %d-%d | inserted=%d dup=%d rejected=%d blank=%d | "
                     "%.0f rows/s", sheet, prev_last + 1, row_num, res["inserted"],
                     res["dup_existing"] + res["dup_workbook"], rejected, blank,
                     n / dt if dt else 0)
        rejected_total += rejected
        prev_last = row_num
        good, issues, seen = [], [], {}
        n = blank = rejected = dup_batch = 0
        t_batch = time.perf_counter()
        if st.max_rejects >= 0 and rejected_total > st.max_rejects:
            raise FatalError(f"Rejected rows ({rejected_total}) exceed --max-rejects "
                             f"{st.max_rejects}; stopped. See import_issues for details.")

    for row in rows:
        row_num += 1
        n += 1
        try:
            empty = set(row) <= EMPTY
        except TypeError:
            empty = False
        if empty:
            blank += 1
        else:
            try:
                vals = convert(row)
            except RowError as e:
                rejected += 1
                if rejected <= 20:
                    log.warning("REJECT sheet='%s' row=%d id=%s reason=%s", sheet, row_num,
                                row_id_text(row), e)
                if loader.take_budget():
                    issues.append((job_id, sheet, row_num, row_id_text(row), "REJECTED", str(e)))
            else:
                key = vals[0]
                first = seen.get(key)
                if first is not None:
                    dup_batch += 1
                    if loader.take_budget():
                        issues.append((job_id, sheet, row_num, str(key)[:200], "DUPLICATE",
                                       f"id repeated within batch (first seen at row {first})"))
                else:
                    seen[key] = row_num
                    vals.append(row_num)
                    good.append(vals)
        if n >= batch_size:
            flush()
    if n:
        flush()
