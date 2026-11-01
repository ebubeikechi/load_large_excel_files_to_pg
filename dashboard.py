from __future__ import annotations

import time

from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from common import fmt_duration, fmt_size

STATUS_STYLE = {"pending": "dim", "loading": "yellow", "validated": "cyan",
                "running": "bold green", "completed": "green", "failed": "bold red"}
STATUS_LABEL = {"pending": "PENDING", "loading": "READING", "validated": "VALIDATED",
                "running": "PROCESSING", "completed": "COMPLETED", "failed": "FAILED"}


class Dashboard:
    def __init__(self, job: dict, file_size: int, db_version: str, file_idx: int, file_total: int):
        self.job, self.file_size, self.db_version = job, file_size, db_version
        self.file_idx, self.file_total = file_idx, file_total
        self.t_start = time.time()
        self.baseline: int | None = None
        self._t_first: float | None = None
        self._done_first = 0
        self.stage_override: str | None = None

    @staticmethod
    def totals(sheets):
        done = sum((s["rows_read"] or 0) + (s["blank"] or 0) for s in sheets)
        known = all(s["total_rows"] is not None for s in sheets)
        total = sum(s["total_rows"] or 0 for s in sheets) if known else None
        return done, total

    def stage(self, sheets) -> str:
        if self.stage_override:
            return self.stage_override
        st = {s["status"] for s in sheets}
        if "running" in st:
            return "Streaming rows -> PostgreSQL COPY"
        if "validated" in st:
            return "Validating headers (waiting for all worksheets)"
        if "failed" in st:
            return "Stopping (error)"
        if st == {"completed"}:
            return "Finalizing"
        return "Reading workbook (parsing worksheets)"

    def render(self, sheets):
        now = time.time()
        done, total = self.totals(sheets)
        if self.baseline is None:
            self.baseline = done
        if done > self.baseline and self._t_first is None:
            self._t_first, self._done_first = now, done
        speed = None
        if self._t_first and now - self._t_first >= 2 and done > self._done_first:
            speed = (done - self._done_first) / (now - self._t_first)
        eta = (total - done) / speed if (speed and total is not None) else None
        ins = sum(s["inserted"] for s in sheets)
        dup = sum(s["dup_existing"] + s["dup_workbook"] for s in sheets)
        rej = sum(s["rejected"] for s in sheets)

        head = Table.grid(padding=(0, 2))
        head.add_column(style="bold cyan")
        head.add_column()
        head.add_row("Workbook", f"{self.job['file_name']}  ({fmt_size(self.file_size)})"
                     + (f"   [file {self.file_idx}/{self.file_total}]" if self.file_total > 1 else ""))
        head.add_row("Database", f"{self.job['db_name']}  [green]connected[/] (PostgreSQL {self.db_version})")
        head.add_row("Destination", f"{self.job['schema_name']}.{self.job['table_name']}")
        head.add_row("Job", self.job["job_id"])
        head.add_row("Worksheets", str(len(sheets)))
        head.add_row("Stage", f"[yellow]{self.stage(sheets)}[/]")

        st = Table(show_header=True, header_style="bold", expand=True)
        for c, j in (("#", "right"), ("Worksheet", "left"), ("Status", "left"),
                     ("Progress", "left"), ("Done / Total", "right"), ("Inserted", "right"),
                     ("Dups", "right"), ("Rejected", "right")):
            st.add_column(c, justify=j)
        for s in sheets:
            d = (s["rows_read"] or 0) + (s["blank"] or 0)
            t = s["total_rows"]
            bar = ProgressBar(total=t if t else None, completed=min(d, t) if t else 0, width=24)
            if s["status"] == "completed" and t:
                bar = ProgressBar(total=t, completed=t, width=24)
            st.add_row(str(s["sheet_order"] + 1), s["sheet_name"],
                       Text(STATUS_LABEL.get(s["status"], s["status"]),
                            style=STATUS_STYLE.get(s["status"], "")),
                       bar, f"{d:,} / {t:,}" if t is not None else f"{d:,} / ?",
                       f"{s['inserted']:,}", f"{s['dup_existing'] + s['dup_workbook']:,}",
                       f"{s['rejected']:,}")

        pct = f"{100 * done / total:6.2f}%" if total else "  ...  "
        overall = Table.grid(padding=(0, 2))
        overall.add_row(Text("Overall", style="bold"),
                        ProgressBar(total=total if total else None,
                                    completed=min(done, total) if total else 0, width=50),
                        Text(pct, style="bold"))

        m = Table.grid(padding=(0, 3))
        m.add_column(style="bold cyan"); m.add_column(justify="right")
        m.add_column(style="bold cyan"); m.add_column(justify="right")
        m.add_row("Rows processed", f"{done:,}", "Elapsed", fmt_duration(now - self.t_start))
        m.add_row("Rows inserted", f"{ins:,}", "Speed",
                  f"{speed:,.0f} rows/sec" if speed else "measuring...")
        m.add_row("Duplicates skipped", f"{dup:,}", "Est. remaining", fmt_duration(eta))
        m.add_row("Rejected records", f"{rej:,}", "Total est. rows",
                  f"{total:,}" if total is not None else "estimating...")
        m.add_row("Remaining rows", f"{max(total - done, 0):,}" if total is not None else "?", "", "")
        return Panel(Group(head, "", st, "", overall, "", m),
                     title="[bold white on blue] EXCEL2POSTGRES [/]", border_style="blue")
