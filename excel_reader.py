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


# ------------------------------------------------------------------ fast header read
import re as _re
import zipfile as _zipfile
from xml.etree import ElementTree as _ET

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class _NoFast(Exception):
    pass


def _col_idx(ref: str | None) -> int:
    m = _re.match(r"[A-Z]+", ref or "")
    if not m:
        raise _NoFast()
    n = 0
    for ch in m.group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _shared_strings(z, need: set[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    if not need:
        return out
    last = max(need)
    with z.open("xl/sharedStrings.xml") as f:
        i = -1
        for _, el in _ET.iterparse(f, events=("end",)):
            if el.tag == _M + "si":
                i += 1
                if i in need:
                    parts = []
                    for ch in el:
                        if ch.tag == _M + "t":
                            parts.append(ch.text or "")
                        elif ch.tag == _M + "r":
                            parts.extend(x.text or "" for x in ch if x.tag == _M + "t")
                    out[i] = "".join(parts)
                el.clear()
                if i >= last:
                    break
    return out


def _fast_first_row(path) -> list[str]:
    """Reads ONLY the first non-empty row of the first worksheet straight from the XLSX XML
    (about a second, even for a multi-GB workbook)."""
    with _zipfile.ZipFile(path) as z:
        wb = _ET.fromstring(z.read("xl/workbook.xml"))
        sheets = [(s.get("name"), s.get(_R + "id")) for s in wb.iter(_M + "sheet")]
        if not sheets:
            raise _NoFast()
        rid = sheets[0][1]
        rels = _ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels if r.get("Id") == rid)
        member = target.lstrip("/") if target.startswith("/") else "xl/" + target
        cells: dict[int, tuple[str | None, str]] = {}
        with z.open(member) as f:
            for _, el in _ET.iterparse(f, events=("end",)):
                if el.tag == _M + "c":
                    t = el.get("t")
                    v = el.find(_M + "v")
                    if t == "inlineStr":
                        text = "".join(x.text or "" for x in el.iter(_M + "t"))
                    elif v is None:
                        text = None
                    elif t in ("s", "str", "e"):
                        text = v.text or ""
                    else:
                        raise _NoFast()      # numeric/boolean header cell -> use exact path
                    if text:
                        cells[_col_idx(el.get("r"))] = (t, text)
                elif el.tag == _M + "row":
                    if cells:
                        break
        if not cells:
            raise _NoFast()
        sst = _shared_strings(z, {int(x) for t, x in cells.values() if t == "s"})
        lo, hi = min(cells), max(cells)
        row = [""] * (hi - lo + 1)
        for i, (t, text) in cells.items():
            row[i - lo] = sst[int(text)] if t == "s" else text
        return row


def get_columns(path, engine: str = "calamine", fast_only: bool = False) -> list[str] | None:
    """Column names of the first worksheet (duplicate names already suffixed). Uses the
    instant XML reader for .xlsx/.xlsm, else falls back to a full parse (slow on big files)."""
    from validator import normalize_header
    raw = None
    if Path(path).suffix.lower() in (".xlsx", ".xlsm"):
        try:
            raw = _fast_first_row(path)
        except Exception:
            raw = None
    if raw is None:
        if fast_only:
            return None
        names = list_sheets(path, engine)
        s = open_sheet(path, names[0], engine)
        try:
            raw = next(s.rows)
        finally:
            s.close()
    return normalize_header(raw)
