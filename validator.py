from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from common import FatalError, METADATA_COLUMNS

EMPTY = frozenset((None, ""))
INT64 = 2 ** 63 - 1
_INF = float("inf")


class RowError(ValueError):
    """Recoverable record-level error (the row is quarantined, import continues)."""


# ---------------------------------------------------------------- converters
def _text(v):
    c = v.__class__
    if c is str:
        if v == "":
            return None
        if "\x00" in v:
            raise ValueError("contains NUL character")
        return v
    if v is None:
        return None
    if c is int:
        return str(v)
    if c is float:
        if v != v or v in (_INF, -_INF):
            raise ValueError("non-finite number")
        return str(int(v)) if v.is_integer() else repr(v)
    if c is bool:
        return "true" if v else "false"
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    return str(v)


def _numeric(v):
    c = v.__class__
    if c is int:
        return v
    if c is float:
        if v != v or v in (_INF, -_INF):
            raise ValueError("non-finite number")
        return v
    if v is None or v == "":
        return None
    if c is str:
        s = v.strip()
        if not s:
            return None
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise ValueError("not a valid number") from None
        if not d.is_finite():
            raise ValueError("non-finite number")
        return d
    if c is Decimal:
        return v
    raise ValueError(f"expected a number, found {c.__name__}")


def _bigint(v):
    c = v.__class__
    if c is int:
        if -INT64 - 1 <= v <= INT64:
            return v
        raise ValueError("integer out of range")
    if v is None or v == "":
        return None
    if c is float and v == v and v not in (_INF, -_INF) and v.is_integer():
        return _bigint(int(v))
    raise ValueError("not a valid integer")


def _date(v):
    c = v.__class__
    if c is date:
        return v
    if c is datetime:
        if v.hour or v.minute or v.second or v.microsecond:
            raise ValueError("date has a time component")
        return v.date()
    if v is None or v == "":
        return None
    if c is str:
        try:
            return date.fromisoformat(v.strip())
        except ValueError:
            raise ValueError("not a valid date") from None
    raise ValueError(f"expected a date, found {c.__name__}")


def _timestamp(v):
    c = v.__class__
    if c is datetime:
        return v
    if c is date:
        return datetime(v.year, v.month, v.day)
    if v is None or v == "":
        return None
    if c is str:
        try:
            return datetime.fromisoformat(v.strip())
        except ValueError:
            raise ValueError("not a valid timestamp") from None
    raise ValueError(f"expected a timestamp, found {c.__name__}")


def _bool(v):
    if v.__class__ is bool:
        return v
    if v is None or v == "":
        return None
    raise ValueError("not a valid boolean")


def _time(v):
    if v.__class__ is time:
        return v
    if v is None or v == "":
        return None
    raise ValueError("not a valid time")


def _interval(v):
    if v.__class__ is timedelta:
        return v
    if v is None or v == "":
        return None
    raise ValueError("not a valid duration")


CONVERTERS = {"TEXT": _text, "NUMERIC": _numeric, "BIGINT": _bigint, "DATE": _date,
              "TIMESTAMP": _timestamp, "BOOLEAN": _bool, "TIME": _time, "INTERVAL": _interval}


def _id_wrapper(conv):
    def wrapped(v):
        if v is None or (v.__class__ is str and not v.strip()):
            raise ValueError("missing id")
        return conv(v)
    return wrapped


class RowConverter:
    """Validates + converts one source row into a list ready for COPY.
    header/kinds describe the IMPORTED columns; indices (if given) are their positions in the
    full source row and full_width is the number of columns in the source header."""

    def __init__(self, header: list[str], kinds: list[str], indices: list[int] | None = None,
                 full_width: int | None = None):
        self.header, self.kinds, self.indices = header, kinds, indices
        self.ncols = full_width or len(header)
        self.convs = [CONVERTERS[k] for k in kinds]
        self.convs[0] = _id_wrapper(self.convs[0])

    def convert(self, row) -> list:
        n = len(row)
        ncols = self.ncols
        if n != ncols:
            if n > ncols:
                for v in row[ncols:]:
                    if v is not None and v != "":
                        raise RowError(f"unexpected data beyond column {ncols}")
                row = row[:ncols]
            else:
                row = list(row) + [None] * (ncols - n)
        if self.indices is not None:
            row = [row[i] for i in self.indices]
        try:
            return [c(v) for c, v in zip(self.convs, row)]
        except Exception:
            raise RowError(self._diagnose(row)) from None

    def _diagnose(self, row) -> str:
        for i, (c, v) in enumerate(zip(self.convs, row)):
            try:
                c(v)
            except Exception as e:  # message deliberately never contains the cell value
                return f"column '{self.header[i]}' ({self.kinds[i]}): {e}"
        return "unknown conversion error"


def row_id_text(row) -> str | None:
    try:
        v = row[0]
        return None if v is None else str(v)[:200]
    except Exception:
        return None


# ------------------------------------------------------------------ headers
_ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
             "ninth", "tenth"]


def _ordinal(n: int) -> str:
    return _ORDINALS[n - 1] if n <= len(_ORDINALS) else f"{n}th"


def normalize_header(row) -> list[str]:
    """Strips cells/trailing blanks. Repeated column names get an ordinal suffix in order of
    appearance, e.g. VASFEE, VASFEE -> VASFEE_first, VASFEE_second. The rename is
    deterministic, so every worksheet with the same header gets identical names."""
    cells = ["" if v is None else str(v).strip() for v in row]
    while cells and cells[-1] == "":
        cells.pop()
    if not cells:
        raise FatalError("The header row is empty.")
    totals: dict[str, int] = {}
    for c in cells:
        if c:
            totals[c] = totals.get(c, 0) + 1
    seen: dict[str, int] = {}
    out = []
    for c in cells:
        if c and totals[c] > 1:
            seen[c] = seen.get(c, 0) + 1
            out.append(f"{c}_{_ordinal(seen[c])}")
        else:
            out.append(c)
    return out


def validate_header_names(header: list[str]) -> None:
    if any(h == "" for h in header):
        raise FatalError("Header contains an empty column name.")
    if len(set(header)) != len(header):
        dups = sorted({h for h in header if header.count(h) > 1})
        raise FatalError(f"Duplicate column names in header: {dups}")
    for h in header:
        if len(h.encode("utf-8")) > 63:
            raise FatalError(f"Column name too long for PostgreSQL (63 bytes max): '{h}'")
        if h.lower() in METADATA_COLUMNS:
            raise FatalError(f"Column name '{h}' collides with a reserved metadata column.")


def compare_headers(ref: list[str], header: list[str], sheet: str) -> None:
    if header == ref:
        return
    missing = [h for h in ref if h not in header]
    extra = [h for h in header if h not in ref]
    if not missing and not extra:
        detail = "same columns but in a different order"
    else:
        detail = f"missing={missing} extra={extra}"
    raise FatalError(f"Worksheet '{sheet}' header differs from the reference header: {detail}")
