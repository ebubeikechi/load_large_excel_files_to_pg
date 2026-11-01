"""Excel2Postgres - fast, resumable bulk import of large Excel workbooks into PostgreSQL."""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def _banner():
    console.print(Panel.fit("[bold white]EXCEL2POSTGRES[/]\nHigh-speed resumable Excel -> PostgreSQL "
                            "importer", border_style="blue"))


def _expand(patterns: list[str]) -> list[str]:
    out = []
    for p in patterns:
        p = p.strip().strip('"').strip("'")
        hits = sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
        out.extend(hits or [p])
    return out


def _pick_files() -> list[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        files = filedialog.askopenfilenames(
            title="Select one or more Excel workbooks (processed one after another)",
            filetypes=[("Excel workbooks", "*.xlsx *.xlsm *.xlsb *.xls"), ("All files", "*.*")])
        root.destroy()
        if files:
            return list(files)
    except Exception:
        pass
    raw = console.input("Enter Excel file path(s), separated by ';': ").strip()
    return _expand([p for p in raw.split(";") if p.strip()])


def _settings(args):
    from config import ImportSettings
    st = ImportSettings.from_env()
    if getattr(args, "batch_size", None):
        st.batch_size = args.batch_size
    if getattr(args, "workers", None) is not None:
        st.workers = args.workers
    if getattr(args, "sample_rows", None):
        st.sample_rows = args.sample_rows
    if getattr(args, "max_rejects", None) is not None:
        st.max_rejects = args.max_rejects
    if getattr(args, "engine", None):
        st.engine = args.engine
    if getattr(args, "schema_file", None):
        st.schema_overrides = json.loads(Path(args.schema_file).read_text(encoding="utf-8"))
    if st.batch_size < 1000:
        raise SystemExit("--batch-size must be at least 1000")
    return st


def _dbcfg(args):
    from config import DbConfig
    return DbConfig.from_env().with_overrides(getattr(args, "database", None),
                                              getattr(args, "schema", None))


def cmd_import(args) -> int:
    from common import FatalError, default_table_name, fmt_size
    from db import check_connection
    from manager import run_import
    _banner()
    files = _expand(args.file) if args.file else _pick_files()
    if not files:
        console.print("[red]No files selected.[/]")
        return 2
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        console.print(f"[red]File(s) not found:[/] {missing}")
        return 2
    cfg, st = _dbcfg(args), _settings(args)
    try:
        ver = check_connection(cfg)
    except FatalError as e:
        console.print(f"[bold red]Database connection FAILED:[/] {e}")
        return 2
    console.print(f"[green]Database connection OK[/] - PostgreSQL {ver} "
                  f"({cfg.host}:{cfg.port}/{cfg.name}, schema '{cfg.schema}')")
    console.print(f"Queue ({len(files)} file(s), processed one after another):")
    for i, f in enumerate(files, 1):
        console.print(f"  {i}. {Path(f).name}  ({fmt_size(Path(f).stat().st_size)})")

    from excel_reader import get_columns
    st.selected_columns = None
    if args.pk_separator:
        st.pk_separator = args.pk_separator
    if args.pk:
        st.pk_columns = [c.strip() for c in args.pk.split(",") if c.strip()]
    if args.columns:
        st.selected_columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    elif not args.all_columns and not args.pk:
        from column_picker import pick_columns
        try:
            with console.status(f"Reading column names from {Path(files[0]).name} ..."):
                cols = get_columns(files[0], st.engine)
        except FatalError as e:
            console.print(f"[bold red]Could not read the header:[/] {e}")
            return 2
        picked = pick_columns(cols, console, st.pk_separator)
        if picked is None:
            console.print("[yellow]Cancelled - nothing was imported.[/]")
            return 2
        chosen, pk = picked
        if len(chosen) < len(cols):
            st.selected_columns = chosen
        if pk != [cols[0]]:
            st.pk_columns = pk
    if st.selected_columns:
        console.print(f"[bold]Columns selected:[/] {len(st.selected_columns)}")
    if st.pk_columns and len(st.pk_columns) > 1:
        console.print(f"[bold]Primary key (composite):[/] {' + '.join(st.pk_columns)} -> column "
                      f"'{'_'.join(st.pk_columns)}' (values joined with '{st.pk_separator}')")
    else:
        console.print(f"[bold]Primary key:[/] {(st.pk_columns or ['first column'])[0]}")
    need = (st.selected_columns or []) + (st.pk_columns or [])
    if need:
        for f in files[1:]:                     # instant pre-check of the other workbooks
            hdr = get_columns(f, st.engine, fast_only=True)
            miss = [c for c in need if hdr is not None and c not in hdr]
            if miss:
                console.print(f"[yellow]Warning:[/] {Path(f).name} lacks column(s) "
                              f"{miss[:5]} - that file will fail validation.")

    outcomes = []
    for i, f in enumerate(files, 1):
        table = args.table or default_table_name(f)
        console.rule(f"[bold]File {i}/{len(files)}: {Path(f).name}  ->  {cfg.schema}.{table}")
        try:
            rep = run_import(cfg, st, f, table, console, idx=i, total=len(files))
            outcomes.append((Path(f).name, "OK", rep["inserted"], rep["duplicates_skipped"],
                             rep["rejected"], rep["duration_seconds"], rep["job_id"]))
        except FatalError as e:
            console.print(Panel(str(e), title="[bold red]IMPORT STOPPED", border_style="red"))
            outcomes.append((Path(f).name, "FAILED", 0, 0, 0, 0, ""))
            if args.stop_on_error:
                break
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted. Committed data is safe - use "
                          "'python main.py jobs' then 'resume --job-id <id>'.[/]")
            return 130
    if len(files) > 1:
        t = Table(title="All files", show_lines=False)
        for c in ("File", "Result", "Inserted", "Duplicates", "Rejected", "Seconds", "Job"):
            t.add_column(c)
        for o in outcomes:
            t.add_row(o[0], f"[green]{o[1]}[/]" if o[1] == "OK" else f"[red]{o[1]}[/]",
                      f"{o[2]:,}", f"{o[3]:,}", f"{o[4]:,}", f"{o[5]:,.0f}", o[6])
        console.print(t)
    return 0 if all(o[1] == "OK" for o in outcomes) else 1


def cmd_resume(args) -> int:
    from common import FatalError
    from manager import run_import
    _banner()
    cfg, st = _dbcfg(args), _settings(args)
    try:
        run_import(cfg, st, args.file, None, console, resume_job_id=args.job_id)
        return 0
    except FatalError as e:
        console.print(Panel(str(e), title="[bold red]RESUME STOPPED", border_style="red"))
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted again - you can resume later.[/]")
        return 130


def _connect_meta(args):
    from checkpoint import Meta, ensure_meta
    from common import FatalError
    from db import check_connection, connect
    cfg = _dbcfg(args)
    try:
        check_connection(cfg)
    except FatalError as e:
        console.print(f"[bold red]Database connection FAILED:[/] {e}")
        raise SystemExit(2)
    conn = connect(cfg, tune=False)
    meta = Meta(conn, cfg.schema)
    ensure_meta(conn, meta)
    return conn, meta


def cmd_status(args) -> int:
    from checkpoint import get_job, get_sheets
    conn, meta = _connect_meta(args)
    job = get_job(conn, meta, args.job_id)
    if not job:
        console.print(f"[red]Job {args.job_id} not found.[/]")
        return 1
    console.print(f"[bold]{job['job_id']}[/]  status=[bold]{job['status']}[/]  file={job['file_name']}  "
                  f"table={job['schema_name']}.{job['table_name']}\n  started={job['started_at']}  "
                  f"updated={job['updated_at']}  error={job['error'] or '-'}")
    t = Table()
    for c in ("#", "Worksheet", "Status", "Last row", "Read", "Inserted", "Dup (existing)",
              "Dup (workbook)", "Rejected", "Blank"):
        t.add_column(c, justify="left" if c in ("Worksheet", "Status") else "right")
    for s in get_sheets(conn, meta, args.job_id):
        t.add_row(str(s["sheet_order"] + 1), s["sheet_name"], s["status"], f"{s['last_row']:,}",
                  f"{s['rows_read']:,}", f"{s['inserted']:,}", f"{s['dup_existing']:,}",
                  f"{s['dup_workbook']:,}", f"{s['rejected']:,}", f"{s['blank']:,}")
    console.print(t)
    if job["status"] in ("running", "interrupted", "failed"):
        console.print(f"Resume with:  python main.py resume --job-id {job['job_id']}")
    return 0


def cmd_jobs(args) -> int:
    from checkpoint import list_jobs
    conn, meta = _connect_meta(args)
    t = Table(title="Import jobs")
    for c in ("Job", "File", "Table", "Status", "Inserted", "Rejected", "Started"):
        t.add_column(c)
    for j in list_jobs(conn, meta):
        t.add_row(j["job_id"], j["file_name"] or "", j["table_name"] or "", j["status"],
                  f"{j['inserted']:,}", f"{j['rejected']:,}", str(j["started_at"])[:19])
    console.print(t)
    return 0


def cmd_validate(args) -> int:
    """Read-only inspection: header consistency, row counts, inferred column types."""
    from itertools import islice
    from common import FatalError
    from excel_reader import list_sheets, open_sheet
    from schema import infer_types
    from validator import compare_headers, normalize_header, validate_header_names
    _banner()
    st = _settings(args)
    rc = 0
    for f in _expand(args.file) if args.file else _pick_files():
        console.rule(f"[bold]{Path(f).name}")
        try:
            names = list_sheets(f, st.engine)
            console.print("Worksheets: " + ", ".join(names))
            ref = kinds = None
            for n in names:
                s = open_sheet(f, n, st.engine)
                hdr = normalize_header(next(s.rows))
                validate_header_names(hdr)
                if ref is None:
                    ref = hdr
                    kinds = infer_types(hdr, list(islice(s.rows, st.sample_rows)),
                                        st.schema_overrides)
                else:
                    compare_headers(ref, hdr, n)
                console.print(f"  [green]OK[/] {n}: {max(s.height - 1, 0):,} data rows, "
                              f"{len(hdr)} columns, header matches")
                s.close()
            t = Table(title="Proposed PostgreSQL schema (override with --schema-file)")
            t.add_column("Column"); t.add_column("Type")
            for h, k in zip(ref, kinds):
                t.add_row(h, k)
            t.add_row("source_sheet", "TEXT (metadata)")
            t.add_row("imported_at", "TIMESTAMPTZ (metadata)")
            console.print(t)
        except FatalError as e:
            console.print(f"[bold red]VALIDATION FAILED:[/] {e}")
            rc = 1
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--database", help="override DB_NAME")
        sp.add_argument("--schema", help="override DB_SCHEMA")

    def tuning(sp):
        sp.add_argument("--batch-size", type=int, help="rows per COPY batch (default 50000)")
        sp.add_argument("--workers", type=int, help="parallel worksheet processes (0 = auto)")
        sp.add_argument("--engine", choices=["calamine", "openpyxl"],
                        help="calamine = fastest; openpyxl = lowest memory")
        sp.add_argument("--max-rejects", type=int, help="stop after N rejected rows (0 = fail-fast)")
        sp.add_argument("--sample-rows", type=int, help="rows sampled for type inference")
        sp.add_argument("--schema-file", help="JSON file {\"column\": \"TEXT|NUMERIC|DATE|...\"}")

    sp = sub.add_parser("import", help="import one or more workbooks (one after another)")
    sp.add_argument("--file", "-f", nargs="+", help="workbook path(s) / wildcards; omit to open a file picker")
    sp.add_argument("--table", "-t", help="destination table (default: derived from file name)")
    sp.add_argument("--stop-on-error", action="store_true", help="stop the queue if a file fails")
    sp.add_argument("--columns", help="comma-separated column names to import (skips the popup)")
    sp.add_argument("--all-columns", action="store_true", help="import every column (skips the popup)")
    sp.add_argument("--pk", help="primary key column(s), comma-separated; several are concatenated in this order")
    sp.add_argument("--pk-separator", help="separator for a composite key (default |)")
    common(sp); tuning(sp); sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("resume", help="resume an interrupted job")
    sp.add_argument("--job-id", required=True)
    sp.add_argument("--file", help="new location of the workbook (checksum must match)")
    common(sp); tuning(sp); sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("status", help="show job status"); sp.add_argument("--job-id", required=True)
    common(sp); sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("jobs", help="list jobs"); common(sp); sp.set_defaults(fn=cmd_jobs)
    sp = sub.add_parser("validate", help="inspect workbook(s) without importing")
    sp.add_argument("--file", "-f", nargs="+"); tuning(sp); sp.set_defaults(fn=cmd_validate)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
