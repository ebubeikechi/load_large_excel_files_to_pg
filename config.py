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
    batch_size: int = 50_000          # rows per COPY batch / checkpoint
    workers: int = 0                  # 0 = auto (one process per worksheet, max 4)
    sample_rows: int = 20_000         # rows used for column type inference
    max_rejects: int = -1             # -1 = unlimited, 0 = fail on first bad row
    max_issue_rows: int = 100_000     # cap on stored issue detail rows per worksheet
    engine: str = "calamine"          # calamine (fast) | openpyxl (lowest memory)
    schema_overrides: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ImportSettings":
        return cls(
            batch_size=int(os.getenv("BATCH_SIZE", "50000")),
            workers=int(os.getenv("WORKERS", "0")),
            sample_rows=int(os.getenv("SAMPLE_ROWS", "20000")),
        )
