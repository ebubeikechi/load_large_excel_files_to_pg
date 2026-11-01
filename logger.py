from __future__ import annotations

import logging

from config import LOG_DIR


def get_logger(job_id: str | None = None) -> logging.Logger:
    """One log file per import job (logs/<job_id>.log). Never logs credentials."""
    name = f"excel2pg.{job_id}" if job_id else "excel2pg.app"
    log = logging.getLogger(name)
    if not log.handlers:
        log.setLevel(logging.DEBUG)
        log.propagate = False
        fh = logging.FileHandler(LOG_DIR / f"{job_id or 'app'}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(processName)s | %(message)s"))
        log.addHandler(fh)
    return log
