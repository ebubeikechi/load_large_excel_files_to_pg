from __future__ import annotations

import hashlib
import re
from pathlib import Path


class FatalError(Exception):
    """Error that must stop the import (shown to the user without a traceback)."""


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
META_TABLES = {"import_jobs", "import_job_sheets", "import_issues"}
METADATA_COLUMNS = ("source_sheet", "imported_at")


def validate_identifier(name: str, what: str) -> str:
    if not IDENT_RE.match(name or ""):
        raise FatalError(f"Invalid {what} '{name}': use letters, digits and underscores "
                         f"only (max 63 chars, must not start with a digit).")
    return name


def validate_table_name(name: str) -> str:
    validate_identifier(name, "table name")
    if name.lower() in META_TABLES:
        raise FatalError(f"'{name}' is reserved for Excel2Postgres bookkeeping.")
    return name


def default_table_name(path: str | Path) -> str:
    stem = re.sub(r"[^a-z0-9_]+", "_", Path(path).stem.lower()).strip("_") or "import"
    if stem[0].isdigit():
        stem = "t_" + stem
    stem = stem[:63]
    return "t_" + stem[:60] if stem in META_TABLES else stem


def fingerprint(path: str | Path) -> str:
    """Full-file BLAKE2b checksum (fast: roughly disk speed)."""
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        return "--:--:--"
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"
