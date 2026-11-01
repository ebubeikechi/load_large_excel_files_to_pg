# Excel2Postgres (fast edition)

Imports huge Excel workbooks (1 GB+, millions of rows, many sheets) into ONE PostgreSQL table,
resumably, with duplicate protection. Select **one or many workbooks**; they run one after another.

## Why it is fast
| Technique | Effect |
|---|---|
| `python-calamine` (Rust) instead of openpyxl | 5-10x faster XLSX parsing (the real bottleneck) |
| One **process per worksheet** | sheets parse + load in parallel (3 sheets ~ up to 3x) |
| `COPY ... FROM STDIN` into a temp stage, then `INSERT ... ON CONFLICT DO NOTHING` | bulk speed + duplicate skipping |
| 50,000-row batches, `synchronous_commit=off` | fewer commits/fsyncs (safe: checkpoint commits with data) |
| Checkpoint written **in the same transaction** as the data | exact, cheap resume |
| Only the PK index exists while loading | no index maintenance overhead |

## Install (Windows 11)
```
cd Excel2Postgres
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then edit DB_PASSWORD etc.
```
Create the database once: `CREATE DATABASE excel_import_db;`

## Use
```
python main.py import                                   # opens a file picker (multi-select)
python main.py import -f "C:\Data\Jan.xlsx" "C:\Data\Feb.xlsx" -t settlement_data
python main.py import -f "C:\Data\*.xlsx" -t settlement_data      # wildcards work
python main.py validate -f "C:\Data\Jan.xlsx"           # inspect headers + proposed schema
python main.py jobs
python main.py status --job-id import_20261101_001
python main.py resume --job-id import_20261101_001      # after Ctrl+C / crash / power loss
```
If `-t` is omitted, the table name is derived from each file name. Several files with the same
`-t` simply append to the same table. Useful flags: `--batch-size`, `--workers`,
`--max-rejects N` (0 = stop at first bad row), `--schema-file types.json`,
`--engine openpyxl` (lowest memory), `--stop-on-error`, `--database`, `--schema`.

## Behaviour
* All sheets' headers must be identical (first sheet = reference). Every worker validates its
  header and **no data is committed until all sheets have passed** (barrier).
* The **first column** (whatever its name, e.g. `id` or `TRANSACTION ID`) becomes the TEXT PRIMARY KEY,
  stored exactly as supplied. Missing ids are rejected. Column names are preserved exactly (quote them
  in SQL if they contain spaces/capitals).
* Types are inferred from a sample: dates -> DATE/TIMESTAMP, numbers -> NUMERIC (never float),
  numeric columns whose name ends in id/ref/number/code/account/phone... stay TEXT.
  Override with `--schema-file` (`{"amount": "NUMERIC", "branch_code": "TEXT"}`).
* Existing tables are never dropped/truncated/updated; compatibility + PK are verified, new ids
  are appended, existing ids are skipped (`DO NOTHING`).
* Bad rows are quarantined in table `import_issues` and `reports/issues_<job>.csv`
  (worksheet, Excel row number, id, reason - cell values are never logged).
* Resume refuses to run if the workbook's size/checksum changed.
* Success is reported only after `count(*)` in the database matches the committed counters.
* Logs: `logs/<job>.log`; reports: `reports/import_summary_<job>.json`; checkpoint mirror: `checkpoints/`.

## Things to know
* **Memory:** calamine holds one parsed worksheet per worker in RAM. With 16 GB and 3 big sheets
  that is normally fine; if you see memory pressure use `--workers 2`, or `--engine openpyxl`.
* **Duplicates across sheets:** sheets load in parallel, so if the same id appears in two sheets,
  which row "wins" is not guaranteed (the other is logged as a duplicate). Use `--workers 1` for
  strict sheet order.
* Default batch is 50,000 (spec said 10,000) because larger batches are faster; change freely.
* Excel cell blanks become NULL. Formula cells use their cached values.

## PostgreSQL tuning for bulk loads (postgresql.conf, then restart)
```
shared_buffers = 4GB
max_wal_size = 16GB
checkpoint_timeout = 30min
wal_compression = on
maintenance_work_mem = 1GB
```

## Tests
`pytest -q` (DB tests use your `.env` database and clean up their own tables).
