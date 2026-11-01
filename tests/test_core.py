"""Run:  pytest -q      (DB tests auto-skip when PostgreSQL is unreachable)"""
import datetime as dt
import uuid

import pytest
from openpyxl import Workbook
from rich.console import Console

from common import FatalError
from config import DbConfig, ImportSettings
from validator import (RowConverter, RowError, compare_headers, normalize_header,
                       validate_header_names)
from schema import infer_types

HDR = ["id", "account_number", "amount", "txn_date", "note"]


def make_book(path, sheets):
    wb = Workbook(write_only=True)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)


def good(i):
    return [f"ID{i}", 1000 + i, 12.5, dt.date(2026, 1, 1), "n"]


# ---------------------------------------------------------------- unit tests
def test_header_checks():
    compare_headers(HDR, list(HDR), "S")
    with pytest.raises(FatalError):
        compare_headers(HDR, HDR[::-1], "S")
    with pytest.raises(FatalError):
        compare_headers(HDR, HDR[:-1] + ["other"], "S")
    validate_header_names(["TRANSACTION ID", "x"])      # first column may have any name
    with pytest.raises(FatalError):
        validate_header_names(["a", "a"])
    assert normalize_header(["id", "a", None, ""]) == ["id", "a"]


def test_type_inference_keeps_identifiers_text():
    kinds = infer_types(HDR, [good(1), good(2)])
    assert kinds == ["TEXT", "TEXT", "NUMERIC", "DATE", "TEXT"]


def test_row_validation():
    conv = RowConverter(HDR, ["TEXT", "TEXT", "NUMERIC", "DATE", "TEXT"])
    assert conv.convert(good(1))[0] == "ID1"
    with pytest.raises(RowError):
        conv.convert([None, 1, 2, dt.date(2026, 1, 1), "x"])          # missing id
    with pytest.raises(RowError):
        conv.convert(["A", 1, "abc", dt.date(2026, 1, 1), "x"])       # bad number
    assert conv.convert([1001.0, 1, 2, None, None])[0] == "1001"       # id preserved as text


# ----------------------------------------------------------- DB integration
def _cfg():
    return DbConfig.from_env()


@pytest.fixture()
def db():
    from db import check_connection, connect
    cfg = _cfg()
    try:
        check_connection(cfg)
    except FatalError:
        pytest.skip("PostgreSQL not reachable")
    conn = connect(cfg, tune=False)
    tables = []
    yield cfg, conn, tables
    for t in tables:
        conn.execute(f'DROP TABLE IF EXISTS "{cfg.schema}"."{t}"')
    conn.close()


def run(cfg, path, table, **kw):
    from manager import run_import
    st = ImportSettings(batch_size=1000, **kw)
    return run_import(cfg, st, str(path), table, Console(quiet=True))


def test_multi_sheet_import_duplicates_rejects_and_append(db, tmp_path):
    cfg, conn, tables = db
    t = "t_" + uuid.uuid4().hex[:8]
    tables.append(t)
    rows = lambda a, b: [HDR] + [good(i) for i in range(a, b)]
    bad = [None, 1, 2, dt.date(2026, 1, 1), "x"]
    make_book(tmp_path / "a.xlsx", {"S1": rows(0, 2500) + [[None] * 5, bad],
                                    "S2": rows(2500, 5000) + [good(3)]})   # good(3) = dup
    rep = run(cfg, tmp_path / "a.xlsx", t)
    assert rep["inserted"] == 5000 and rep["rejected"] == 1
    assert rep["duplicates_skipped"] == 1 and rep["blank_rows_skipped"] == 1
    assert conn.execute(f'SELECT count(DISTINCT source_sheet) FROM "{t}"').fetchone()[0] == 2
    # append: same IDs are skipped, new ones inserted, nothing overwritten
    make_book(tmp_path / "b.xlsx", {"S1": rows(4990, 5010)})
    rep2 = run(cfg, tmp_path / "b.xlsx", t)
    assert rep2["inserted"] == 10 and rep2["duplicates_already_in_destination"] == 10
    assert conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] == 5010


def test_header_mismatch_stops_before_any_insert(db, tmp_path):
    cfg, conn, tables = db
    t = "t_" + uuid.uuid4().hex[:8]
    tables.append(t)
    make_book(tmp_path / "m.xlsx", {"S1": [HDR, good(1)], "S2": [HDR[:-1] + ["renamed"], good(2)]})
    with pytest.raises(FatalError):
        run(cfg, tmp_path / "m.xlsx", t)
    assert conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] == 0


def test_resume_refused_when_source_changed(db, tmp_path):
    from manager import run_import
    from checkpoint import Meta, set_job_status
    cfg, conn, tables = db
    t = "t_" + uuid.uuid4().hex[:8]
    tables.append(t)
    p = tmp_path / "r.xlsx"
    make_book(p, {"S1": [HDR] + [good(i) for i in range(50)]})
    rep = run(cfg, p, t)
    set_job_status(conn, Meta(conn, cfg.schema), rep["job_id"], "interrupted")
    make_book(p, {"S1": [HDR] + [good(i) for i in range(60)]})        # modify workbook
    with pytest.raises(FatalError, match="CHANGED"):
        run_import(cfg, ImportSettings(), None, None, Console(quiet=True),
                   resume_job_id=rep["job_id"])


def test_column_projection_and_duplicate_header_names():
    conv = RowConverter(["id", "amount"], ["TEXT", "NUMERIC"], indices=[0, 2], full_width=4)
    assert conv.convert(["A1", "skipped", 5, "skipped"]) == ["A1", 5]
    assert conv.convert(["A2", "x", 6]) == ["A2", 6]          # short row padded, then projected
    assert normalize_header(["id", "V", "V", "W"]) == ["id", "V_first", "V_second", "W"]


def test_composite_primary_key_converter():
    from validator import pk_column_name
    conv = RowConverter(["BRANCH", "N", "RRN"], ["TEXT", "NUMERIC", "TEXT"], pk=["RRN", "BRANCH"])
    vals = conv.convert(["B1", 5, "R9"])
    assert vals == ["R9|B1", "B1", 5, "R9"] and conv.key_index == 0     # key FIRST, pk order
    assert RowConverter(["BRANCH", "N", "RRN"], ["TEXT", "NUMERIC", "TEXT"],
                        pk=["RRN", "BRANCH"], sep="").convert(["B1", 5, "R9"])[0] == "R9B1"
    assert pk_column_name(["RRN", "BRANCH"]) == "RRN_BRANCH"
    with pytest.raises(RowError):
        conv.convert([None, 5, "R9"])                                    # missing key part


def test_composite_pk_import_end_to_end(db, tmp_path):
    cfg, conn, tables = db
    t = "t_" + uuid.uuid4().hex[:8]
    tables.append(t)
    make_book(tmp_path / "c.xlsx", {"S": [["BR", "RRN", "AMT"], ["A", "1", 5], ["A", "2", 6],
                                          ["B", "1", 7], ["A", "1", 8]]})
    rep = run(cfg, tmp_path / "c.xlsx", t, pk_columns=["BR", "RRN"])
    cols = [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position", (t,))]
    assert cols[0] == "BR_RRN"                                           # key is the first column
    assert rep["inserted"] == 3 and rep["duplicates_skipped"] == 1
    assert rep["primary_key"]["column_name"] == "BR_RRN"
    keys = {r[0] for r in conn.execute(f'SELECT "BR_RRN" FROM "{t}"').fetchall()}
    assert keys == {"A|1", "A|2", "B|1"}


def test_issues_table_collects_rejected_and_duplicates(db, tmp_path):
    cfg, conn, tables = db
    t = "t_" + uuid.uuid4().hex[:8]
    tables += [t, t + "_issues"]
    make_book(tmp_path / "i.xlsx", {"S": [HDR, good(1), good(2),
                                          ["BAD", 1, "abc", dt.date(2026, 1, 1), "x"],   # bad number
                                          [None, 1, 2, dt.date(2026, 1, 1), "x"],        # no id
                                          good(1)]})                                      # duplicate
    rep = run(cfg, tmp_path / "i.xlsx", t, schema_overrides={"amount": "NUMERIC"})
    assert rep["inserted"] == 2 and rep["rejected"] == 2 and rep["duplicates_skipped"] == 1
    rows = conn.execute(f'SELECT _issue_kind, "id", "amount", _source_row FROM "{t}_issues" '
                        f'ORDER BY _source_row').fetchall()
    assert [r[0] for r in rows] == ["REJECTED", "REJECTED", "DUPLICATE_IN_WORKBOOK"]
    assert rows[0][1] == "BAD" and rows[0][2] == "abc"        # raw bad value kept as text
    assert rows[1][1] is None                                   # missing id still stored


def test_database_errors_are_isolated_into_issues_table(db, tmp_path):
    cfg, conn, tables = db
    t = "t_" + uuid.uuid4().hex[:8]
    tables += [t, t + "_issues"]
    conn.execute(f'CREATE TABLE "{t}" (id TEXT PRIMARY KEY, amt NUMERIC(5,2), '
                 f'source_sheet TEXT NOT NULL, imported_at TIMESTAMPTZ NOT NULL)')
    body = [["id", "amt"]] + [[f"K{i}", 1.5] for i in range(40)]
    body[10] = ["K9", 123456.0]          # numeric overflow in an existing NUMERIC(5,2) column
    body[30] = ["K29", 99999.0]
    make_book(tmp_path / "d.xlsx", {"S": body})
    rep = run(cfg, tmp_path / "d.xlsx", t)
    assert rep["inserted"] == 38 and rep["rejected"] == 2
    bad = conn.execute(f'SELECT id, amt, _issue_kind FROM "{t}_issues" ORDER BY _source_row').fetchall()
    assert bad == [("K9", "123456", "DB_ERROR"), ("K29", "99999", "DB_ERROR")]


def test_empty_separator_and_pk_name_override(db, tmp_path):
    cfg, conn, tables = db
    t = "t_" + uuid.uuid4().hex[:8]
    tables += [t, t + "_issues"]
    make_book(tmp_path / "e.xlsx", {"S": [["A", "B", "V"], ["x", "1", 5], ["y", "2", 6]]})
    run(cfg, tmp_path / "e.xlsx", t, pk_columns=["A", "B"], pk_separator="", pk_column_name="txn_key")
    keys = {r[0] for r in conn.execute(f'SELECT txn_key FROM "{t}"')}
    assert keys == {"x1", "y2"}
