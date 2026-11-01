from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

LOG_DIR = BASE_DIR / "logs"
REPORT_DIR = BASE_DIR / "reports"
CHECKPOINT_DIR = BASE_DIR / "checkpoints"
for _d in (LOG_DIR, REPORT_DIR, CHECKPOINT_DIR):
    _d.mkdir(exist_ok=True)

# =====================================================================================
#  USER SETTINGS  -  edit the values below.
#  Priority:  command-line flag  >  .env file (same NAME)  >  value in this file.
# =====================================================================================

# ---- Primary key -----------------------------------------------------------------
# Text placed BETWEEN the values when several columns form the primary key.
# "|" -> A|B     "-" -> A-B     ""  (empty) -> AB  (plain concatenation, NO separator)
PK_SEPARATOR = "|"

# Name of the combined key column = the key column names joined by this text, in the order
# they are concatenated.  "_" -> BRANCH_RRN     "" -> BRANCHRRN
PK_NAME_JOINER = "_"

# Fixed name for the combined key column. Empty = build it from the column names.
PK_COLUMN_NAME = ""

# Primary key used when you do not pick one (popup / --pk). Empty list = the first column.
# Example: DEFAULT_PK_COLUMNS = ["BRANCH", "RRN"]
DEFAULT_PK_COLUMNS: list = []

# ---- Issues table ----------------------------------------------------------------
# Rows that cannot enter the main table are saved in "<table><suffix>" (all columns TEXT,
# no constraints) together with the reason.
ISSUES_TABLE_SUFFIX = "_issues"
# Also save rows skipped because their primary key already exists / repeats?
ISSUES_INCLUDE_DUPLICATES = True
# If PostgreSQL itself rejects part of a batch (e.g. value too big for an existing
# NUMERIC(10,2) column), find the bad rows, send them to the issues table and still load the
# good ones. False = stop the import on the first database error.
ISOLATE_DB_ERRORS = True

# ---- Speed / resources -----------------------------------------------------------
BATCH_SIZE = 50_000          # rows per COPY batch (= checkpoint size)
WORKERS = 0                  # parallel worksheet processes; 0 = automatic
AUTO_WORKERS_MAX = 3         # cap used by "automatic" (each worker holds a sheet in RAM)
SAMPLE_ROWS = 20_000         # rows inspected to choose column types
ENGINE = "calamine"          # "calamine" (fastest) or "openpyxl" (lowest memory)
SYNCHRONOUS_COMMIT_OFF = True   # faster commits; safe because checkpoints commit with data

# ---- Data rules ------------------------------------------------------------------
MAX_REJECTS = -1             # stop after this many rejected rows; -1 = never, 0 = first bad row
# A numeric-looking column whose LAST name word is in this list is kept as TEXT
# (identifiers, references, phone numbers...) instead of NUMERIC.
TEXT_COLUMN_NAME_TOKENS = [
    "id", "ref", "reference", "no", "num", "number", "code", "acct", "account", "phone",
    "tel", "mobile", "msisdn", "zip", "postal", "stan", "rrn", "pan", "bin", "tin", "bvn",
    "nuban", "iban", "sku", "uuid", "guid", "cheque",
]

# ---- After the import ------------------------------------------------------------
VERIFY_AFTER_IMPORT = True   # count rows in the database and compare with the counters
ANALYZE_AFTER_IMPORT = True  # refresh planner statistics

_SETTING_NAMES = (
    "PK_SEPARATOR", "PK_NAME_JOINER", "PK_COLUMN_NAME", "DEFAULT_PK_COLUMNS",
    "ISSUES_TABLE_SUFFIX", "ISSUES_INCLUDE_DUPLICATES", "ISOLATE_DB_ERRORS", "BATCH_SIZE",
    "WORKERS", "AUTO_WORKERS_MAX", "SAMPLE_ROWS", "ENGINE", "SYNCHRONOUS_COMMIT_OFF",
    "MAX_REJECTS", "TEXT_COLUMN_NAME_TOKENS", "VERIFY_AFTER_IMPORT", "ANALYZE_AFTER_IMPORT",
)


def _apply_env_overrides(ns: dict) -> None:
    for name in _SETTING_NAMES:
        raw = os.getenv(name)
        if raw is None:
            continue
        cur = ns[name]
        if isinstance(cur, bool):
            ns[name] = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            ns[name] = int(raw)
        elif isinstance(cur, list):
            ns[name] = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            ns[name] = raw


_apply_env_overrides(globals())


# =====================================================================================
@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    schema: str = "public"

    @classmethod
    def from_env(cls) -> "DbConfig":
        return cls(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            name=os.getenv("DB_NAME", "excel_import_db"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
            schema=os.getenv("DB_SCHEMA", "public"),
        )

    def with_overrides(self, database: str | None = None, schema: str | None = None) -> "DbConfig":
        return replace(self, name=database or self.name, schema=schema or self.schema)

    def conninfo(self) -> str:
        return make_conninfo(
            host=self.host, port=self.port, dbname=self.name, user=self.user,
            password=self.password, application_name="Excel2Postgres",
        )


@dataclass
class ImportSettings:
    batch_size: int = 50_000
    workers: int = 0
    sample_rows: int = 20_000
    max_rejects: int = -1
    engine: str = "calamine"
    schema_overrides: dict = field(default_factory=dict)
    selected_columns: list | None = None      # None = import every column
    pk_columns: list | None = None            # None = first column; several = concatenated
    pk_separator: str = "|"
    pk_name_joiner: str = "_"
    pk_column_name: str = ""
    issues_suffix: str = "_issues"
    issues_include_duplicates: bool = True
    isolate_db_errors: bool = True
    verify_after_import: bool = True
    analyze_after_import: bool = True

    @classmethod
    def from_env(cls) -> "ImportSettings":
        """Defaults come from the USER SETTINGS block above (and .env overrides)."""
        return cls(
            batch_size=BATCH_SIZE, workers=WORKERS, sample_rows=SAMPLE_ROWS,
            max_rejects=MAX_REJECTS, engine=ENGINE,
            pk_columns=list(DEFAULT_PK_COLUMNS) or None, pk_separator=PK_SEPARATOR,
            pk_name_joiner=PK_NAME_JOINER, pk_column_name=PK_COLUMN_NAME,
            issues_suffix=ISSUES_TABLE_SUFFIX, issues_include_duplicates=ISSUES_INCLUDE_DUPLICATES,
            isolate_db_errors=ISOLATE_DB_ERRORS, verify_after_import=VERIFY_AFTER_IMPORT,
            analyze_after_import=ANALYZE_AFTER_IMPORT,
        )
