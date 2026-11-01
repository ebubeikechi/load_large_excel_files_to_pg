from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from psycopg import sql
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from checkpoint import (Meta, create_job, ensure_meta, get_job, get_sheets, next_job_id,
                        now, set_job_status, touch_job, write_local_checkpoint)
from common import FatalError, fingerprint, fmt_duration, fmt_size, validate_table_name
from config import REPORT_DIR, DbConfig, ImportSettings
from dashboard import Dashboard
from db import check_connection, connect, qname
from excel_reader import SUPPORTED, list_sheets
from logger import get_logger
from schema import table_exists
from worker import run_sheet


class SafeLive(Live):
    """Rich Live that never crashes the import if the Windows console write fails."""

    def refresh(self) -> None:
        try:
            super().refresh()
        except OSError:
            pass


def _terminate(ex) -> None:
    for p in list(getattr(ex, "_processes", {}).values()):
        try:
            p.terminate()
        except Exception:
            pass


def _prepare_new(conn, meta, db_cfg, st, path, table, log, console):
    validate_table_name(table)
    names = list_sheets(path, st.engine)
    if not names:
        raise FatalError("The workbook contains no worksheets.")
    console.print("[bold]Worksheets detected:[/] " + ", ".join(f"[cyan]{n}[/]" for n in names))
    console.print("[dim]Hashing workbook for resume-integrity checks...[/]")
    stat = path.stat()
    job = {"job_id": next_job_id(conn, meta), "file_path": str(path), "file_name": path.name,
           "file_size": stat.st_size, "file_mtime": stat.st_mtime,
           "fingerprint": fingerprint(path), "db_name": db_cfg.name, "schema_name": db_cfg.schema,
           "table_name": table, "batch_size": st.batch_size, "imported_at": now(),
           "started_at": now(), "settings": {k: v for k, v in asdict(st).items()}}
    create_job(conn, meta, job, names)
    return get_job(conn, meta, job["job_id"])


def _prepare_resume(conn, meta, job_id, path_override, console):
    job = get_job(conn, meta, job_id)
    if job is None:
        raise FatalError(f"Job '{job_id}' not found.")
    if job["status"] == "completed":
        raise FatalError(f"Job '{job_id}' already completed; nothing to resume.")
    path = Path(path_override or job["file_path"]).expanduser().resolve()
    if not path.is_file():
        raise FatalError(f"Source workbook not found: {path} (use --file to point to it).")
    console.print(f"[bold yellow]RECOVERY[/] verifying source workbook for job {job_id} ...")
    stat = path.stat()
    if stat.st_size != job["file_size"] or fingerprint(path) != job["fingerprint"]:
        raise FatalError("The source workbook has CHANGED since the job started. Automatic "
                         "resume is refused; start a new import job.")
    if (job["file_mtime"] is not None and abs(stat.st_mtime - job["file_mtime"]) > 1):
        console.print("[yellow]Note: modification time differs but the checksum matches.[/]")
    meta_sheets = get_sheets(conn, meta, job_id)
    done = [s for s in meta_sheets if s["status"] == "completed"]
    for s in meta_sheets:
        if s["status"] != "completed":
            console.print(f"  [cyan]{s['sheet_name']}[/]: resuming after Excel row "
                          f"{s['last_row']:,} ({s['inserted']:,} rows already committed)")
    if len(done):
        console.print(f"  {len(done)} worksheet(s) already completed will be skipped.")
    conn.execute(f"UPDATE {meta.sheets} SET status = 'pending', error = NULL "
                 f"WHERE job_id = %s AND status <> 'completed'", (job_id,))
    set_job_status(conn, meta, job_id, "running")
    return path, job


def run_import(db_cfg: DbConfig, st: ImportSettings, file_path: str, table: str | None,
               console, resume_job_id: str | None = None, idx: int = 1, total: int = 1) -> dict:
    conn = connect(db_cfg, tune=False)
    meta = Meta(conn, db_cfg.schema)
    version = check_connection(db_cfg)
    ensure_meta(conn, meta)
    if resume_job_id:
        path, job = _prepare_resume(conn, meta, resume_job_id, file_path, console)
        table = job["table_name"]
        st.batch_size = job["batch_size"] or st.batch_size
        resumed = True
    else:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FatalError(f"File not found: {path}")
        if path.suffix.lower() not in SUPPORTED:
            raise FatalError(f"Unsupported file type '{path.suffix}'.")
        validate_table_name(table)
        if table_exists(conn, db_cfg.schema, table):
            console.print(f"[yellow]Table {db_cfg.schema}.{table} already exists - new records "
                          f"will be APPENDED (nothing is dropped, truncated or overwritten).[/]")
        job = _prepare_new(conn, meta, db_cfg, st, path, table, log=None, console=console)
        resumed = False
    job_id = job["job_id"]
    log = get_logger(job_id)
    log.info("Job %s started: file=%s (%s) -> %s.%s.%s resumed=%s batch=%d engine=%s",
             job_id, path.name, fmt_size(job["file_size"]), db_cfg.name, db_cfg.schema,
             job["table_name"], resumed, st.batch_size, st.engine)
    log.info("PostgreSQL connected (version %s) host=%s db=%s", version, db_cfg.host, db_cfg.name)

    sheets = get_sheets(conn, meta, job_id)
    pending = [s for s in sheets if s["status"] != "completed"]
    workers = st.workers or min(len(pending) or 1, 3)
    workers = max(1, min(workers, len(pending) or 1))
    barrier = [s["sheet_name"] for s in pending[:workers]]
    log.info("Processing %d worksheet(s) with %d worker process(es)", len(pending), workers)

    dash = Dashboard(job, job["file_size"], version, idx, total)
    t_run = time.time()
    results, failed = [], None
    ex = ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"))
    interrupted = False
    try:
        futs = {}
        for s in pending:
            task = {"db": asdict(db_cfg), "settings": asdict(st), "job_id": job_id,
                    "sheet": s["sheet_name"], "order": s["sheet_order"], "file": str(path),
                    "table": job["table_name"], "barrier": barrier}
            futs[ex.submit(run_sheet, task)] = s["sheet_name"]
        waiting = set(futs)
        done_start = Dashboard.totals(sheets)[0]
        with SafeLive(dash.render(sheets), console=console, auto_refresh=False) as live:
            last_touch = 0.0
            while waiting:
                done, waiting = wait(waiting, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in done:
                    try:
                        res = f.result()
                    except Exception as e:      # worker process died
                        res = {"sheet": futs[f], "ok": False, "error": f"worker crashed: {e!r}"}
                    results.append(res)
                    if not res["ok"] and failed is None:
                        failed = res
                        _terminate(ex)          # stop the others; everything is resumable
                        waiting = set()
                sheets = get_sheets(conn, meta, job_id)
                live.update(dash.render(sheets), refresh=True)
                if time.time() - last_touch > 5:
                    touch_job(conn, meta, job_id)
                    write_local_checkpoint(get_job(conn, meta, job_id), sheets)
                    last_touch = time.time()
            sheets = get_sheets(conn, meta, job_id)
            dash.stage_override = "Verifying committed data" if not failed else "Stopped"
            live.update(dash.render(sheets), refresh=True)
    except KeyboardInterrupt:
        interrupted = True
        _terminate(ex)
        set_job_status(conn, meta, job_id, "interrupted", "Interrupted by user (Ctrl+C)")
        log.warning("Job interrupted by user; resume with: python main.py resume --job-id %s", job_id)
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        if not interrupted:
            ex.shutdown(wait=True, cancel_futures=True)

    if failed:
        msg = f"{failed['sheet']}: {failed['error']}"
        set_job_status(conn, meta, job_id, "failed", msg)
        log.critical("Job %s FAILED: %s", job_id, msg)
        write_local_checkpoint(get_job(conn, meta, job_id), get_sheets(conn, meta, job_id))
        raise FatalError(f"{msg}\n  Committed data is safe. Fix the problem and run:  "
                         f"python main.py resume --job-id {job_id}")

    return _finalize(conn, meta, db_cfg, job, sheets, t_run, done_start, resumed, path, log, console)


def _finalize(conn, meta, db_cfg, job, sheets, t_run, done_start, resumed, path, log, console):
    job_id = job["job_id"]
    dest = qname(conn, db_cfg.schema, job["table_name"])
    db_count = conn.execute(f"SELECT count(*) FROM {dest} WHERE imported_at = %s",
                            (job["imported_at"],)).fetchone()[0]
    inserted = sum(s["inserted"] for s in sheets)
    verified = db_count == inserted
    conn.execute(f"ANALYZE {dest}")
    status = "completed" if verified else "verification_failed"
    set_job_status(conn, meta, job_id, status, None if verified else
                   f"DB holds {db_count} rows for this job but counters say {inserted}", True)
    issues_file = _export_issues(conn, meta, job_id)
    done_end = Dashboard.totals(sheets)[0]
    duration = time.time() - t_run
    rows_read = sum(s["rows_read"] for s in sheets)
    report = {
        "job_id": job_id, "workbook_filename": path.name, "workbook_path": str(path),
        "workbook_size_bytes": job["file_size"], "destination_database": db_cfg.name,
        "destination_table": f"{db_cfg.schema}.{job['table_name']}",
        "worksheets_processed": [{k: s[k] for k in (
            "sheet_name", "rows_read", "inserted", "dup_existing", "dup_workbook", "rejected",
            "blank")} for s in sheets],
        "total_source_rows": rows_read, "inserted": inserted,
        "duplicates_skipped": sum(s["dup_existing"] + s["dup_workbook"] for s in sheets),
        "duplicates_already_in_destination": sum(s["dup_existing"] for s in sheets),
        "duplicates_within_workbook": sum(s["dup_workbook"] for s in sheets),
        "rejected": sum(s["rejected"] for s in sheets),
        "blank_rows_skipped": sum(s["blank"] for s in sheets),
        "verified_rows_in_database": db_count, "verification_passed": verified,
        "start_time": job["started_at"].isoformat(), "completion_time": now().isoformat(),
        "duration_seconds": round(duration, 2),
        "average_rows_per_second": round((done_end - done_start) / duration, 1) if duration else 0,
        "resumed": resumed, "final_status": "SUCCESS" if verified else "VERIFICATION_FAILED",
        "issues_file": str(issues_file) if issues_file else None,
    }
    rpath = REPORT_DIR / f"import_summary_{job_id}.json"
    rpath.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("Job %s %s: %s", job_id, report["final_status"],
             {k: report[k] for k in ("total_source_rows", "inserted", "duplicates_skipped",
                                     "rejected", "blank_rows_skipped", "duration_seconds")})
    write_local_checkpoint(get_job(conn, meta, job_id), sheets)
    print_summary(console, report, rpath)
    if not verified:
        raise FatalError(f"VERIFICATION FAILED: database holds {db_count:,} rows for this job "
                         f"but {inserted:,} were counted as inserted.")
    conn.close()
    return report


def _export_issues(conn, meta, job_id):
    n = conn.execute(f"SELECT count(*) FROM {meta.issues} WHERE job_id = %s", (job_id,)).fetchone()[0]
    if not n:
        return None
    path = REPORT_DIR / f"issues_{job_id}.csv"
    q = sql.SQL("COPY (SELECT sheet_name, row_num, row_id, kind, reason FROM {t} "
                "WHERE job_id = {j} ORDER BY sheet_name, row_num) TO STDOUT WITH (FORMAT CSV, HEADER)"
                ).format(t=sql.SQL(meta.issues), j=sql.Literal(job_id))
    with open(path, "wb") as f, conn.cursor() as cur:
        with cur.copy(q) as cp:
            for data in cp:
                f.write(data)
    return path


def print_summary(console, r: dict, rpath) -> None:
    ok = r["verification_passed"]
    t = Table.grid(padding=(0, 3))
    t.add_column(style="bold"); t.add_column(justify="right")
    t.add_row("Total source records", f"{r['total_source_rows']:,}")
    t.add_row("Inserted", f"{r['inserted']:,}")
    t.add_row("Duplicates skipped", f"{r['duplicates_skipped']:,}  "
              f"(existing: {r['duplicates_already_in_destination']:,}, in workbook: "
              f"{r['duplicates_within_workbook']:,})")
    t.add_row("Rejected", f"{r['rejected']:,}")
    t.add_row("Blank rows skipped", f"{r['blank_rows_skipped']:,}")
    t.add_row("Verified in database", f"{r['verified_rows_in_database']:,}")
    t.add_row("Destination", r["destination_table"])
    t.add_row("Duration", fmt_duration(r["duration_seconds"]))
    t.add_row("Average speed", f"{r['average_rows_per_second']:,.0f} rows/sec")
    t.add_row("Report", str(rpath))
    if r.get("issues_file"):
        t.add_row("Rejected/duplicate detail", r["issues_file"])
    console.print(Panel(t, title="[bold green]IMPORT COMPLETED SUCCESSFULLY" if ok
                        else "[bold red]IMPORT FINISHED - VERIFICATION FAILED",
                        border_style="green" if ok else "red"))
