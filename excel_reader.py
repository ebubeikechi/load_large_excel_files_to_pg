from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

SUPPORTED = {".xlsx", ".xlsm", ".xlsb", ".xls"}


class SheetStream:
    """rows: iterator of row sequences. first_row: Excel row number of the first row
    yielded (the header). height: number of rows in the used range (0 if unknown)."""

    def __init__(self, rows: Iterator, first_row: int, height: int,
                 closer: Callable[[], None] | None = None):
        self.rows, self.first_row, self.height, self._closer = rows, first_row, height, closer

    def close(self) -> None:
        if self._closer:
            try:
                self._closer()
            except Exception:
                pass


def list_sheets(path: str | Path, engine: str = "calamine") -> list[str]:
    if engine == "openpyxl":
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()
    from python_calamine import CalamineWorkbook
    wb = CalamineWorkbook.from_path(str(path))
    try:
        return list(wb.sheet_names)
    finally:
        if hasattr(wb, "close"):
            wb.close()


def open_sheet(path: str | Path, name: str, engine: str = "calamine") -> SheetStream:
    if engine == "openpyxl":
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True, data_only=True)
        ws = wb[name]
        return SheetStream(ws.iter_rows(values_only=True), 1, int(ws.max_row or 0), wb.close)

    from python_calamine import CalamineWorkbook
    wb = CalamineWorkbook.from_path(str(path))
    sheet = wb.get_sheet_by_name(name)
    start = getattr(sheet, "start", None)
    first_row = (start[0] + 1) if start else 1
    return SheetStream(sheet.iter_rows(), first_row, int(sheet.height or 0),
                       getattr(wb, "close", None))
