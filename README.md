# sql-dedatabaser

Extract tables and rows from SQL dump files into CSV — with a focus on
**credentials (usernames/passwords)** and **personally identifiable information
(PII)**. Point it at a folder of `.sql` dumps and it produces, per file, a folder of
CSVs containing just the columns worth keeping.

It has two engines:

- **Serverless** (default) — parses dumps directly, **no database server required**.
  Tolerant of non-standard/malformed files and multiple SQL dialects.
- **Live-replay** — replays each dump into a real MySQL server and reads it back.

> **Intended use:** for authorized security research, incident response, and analysis
> of data you have the right to process. You are responsible for how you use it.

---

## How it works

The serverless engine reads each statement and extracts data in two tiers, so a messy
file still yields rows:

1. **Structural** — parse with [`sqlglot`](https://github.com/tobymao/sqlglot) and read
   `CREATE TABLE` / `INSERT` nodes straight from the AST (handles MySQL, PostgreSQL,
   MSSQL, SQLite, …).
2. **Salvage** — when a statement won't parse, a tolerant regex/scanner recovers
   `INSERT` rows anyway (respecting quotes, escapes, nested parens, embedded
   commas/newlines/semicolons).

Both tiers are fed by a robust statement splitter that respects string literals,
comment styles, and `DELIMITER` directives. Extracted values preserve SQL `NULL`
distinctly (empty field, not the text `"None"`), and binary/BLOB data is kept
losslessly.

---

## Requirements

- **Python 3.10+**
- Dependencies (see [`requirements.txt`](requirements.txt)):
  - `alive-progress` — progress bar (required)
  - `sqlglot` — structural SQL parsing (recommended; without it you get salvage-only)
  - `charset-normalizer` — encoding detection for non-UTF-8 dumps (recommended)
  - `mysql-connector-python` — **only** for the live-replay engine

## Installation

```bash
git clone <your-fork-url> sql-dedatabaser
cd sql-dedatabaser
python -m pip install -r requirements.txt
```

Use `python3`/`pip3` if that's how Python is invoked on your system.

---

## Quick start

1. Put your dump file(s) in an input folder, e.g. `dumps/`.
2. Run it (the output folders are created automatically):

```bash
python main.py --input dumps --done output/done --errors output/errors
```

For `dumps/backup.sql` you'll get:

```text
output/done/backup/
├── users.csv          # one CSV per matched table, named after the table
├── accounts.csv
└── backup.sql         # the original dump, kept for provenance
```

---

## What gets extracted (the filter)

By default the tool scans **all tables** and extracts columns whose names look like
**credentials or PII**. Matching is case-insensitive and substring-based, so `pass`
catches `password`/`passwd`, `name` catches `first_name`, and so on.

Built-in patterns cover:

| Category | Examples |
|----------|----------|
| Credentials | user, pass, pwd, login, secret, token, auth, session, pin, hash, salt, api_key, otp |
| Identity / contact | email, name, phone, mobile, address, city, state, zip, postal, country |
| Government IDs | ssn, social, national, passport, license, tax, tin |
| Financial | card, credit, debit, cvv, iban, swift, account, routing, bank |
| Demographic | dob, birth, gender, sex |

The full list is `DEFAULT_CREDENTIAL_PATTERNS` + `DEFAULT_PII_PATTERNS` in
[`main.py`](main.py). Matching is by column **name**, not content: a column whose name
matches is included even if some of its rows are `NULL` (those cells are simply blank).

Override the filter from the command line — no code editing:

```bash
# only user/account tables, only password + email columns
python main.py --tables "user,account" --columns "pass,email" --input dumps --done output/done --errors output/errors

# every column of every table (full dump to CSV)
python main.py --columns ".*" --input dumps --done output/done --errors output/errors
```

---

## Output routing

Each processed file ends up in one of three places:

| Result | Destination | Meaning |
|--------|-------------|---------|
| Data extracted | `--done/<name>/` | A folder of CSVs (one per table) plus the original dump |
| Clean, no matches | `--no-hits` (or `--errors`) | Parsed fine, but no table had a credential/PII column |
| Parse failure | `--errors` | Unreadable file, or too many SQL errors |

Setting `--no-hits` keeps "nothing to extract here" separate from "this file is
broken", which makes triaging a large batch much easier:

```bash
python main.py --input dumps --done output/done --errors output/errors --no-hits output/no-hits
```

---

## The live-replay engine

Use `--engine live` when you'd rather a real MySQL server do the parsing (maximally
tolerant of unusual dialects, and it executes procedural SQL the serverless reader
skips).

> ⚠️ **Warning:** this engine **drops and recreates a database named `test`** on the
> target server every run. Don't point it at a server where `test` holds anything you
> care about.

1. Install the connector and have a MySQL server reachable:

   ```bash
   python -m pip install mysql-connector-python
   ```

2. Set your server credentials in the `connector_cfg` block in [`main.py`](main.py)
   (defaults are `root` / `password` / `localhost`).

3. Run with `--engine live`:

   ```bash
   python main.py --engine live --input dumps --done output/done --errors output/errors
   ```

Output routing and the filter behave exactly as with the serverless engine.

**Which engine to use:** start with **serverless** — it needs no server and preserves
rows exactly as written in the dump. Reach for `--engine live` only if a file's dialect
defeats the serverless parser or you specifically need server-side execution.

---

## Library usage (single file)

For one file with a custom filter, the parser is usable directly.
`write_results_to_dir` gives the same folder-of-CSVs output:

```python
from serverless_parser import ServerlessSqlParser
from dbparser import DatabaseFilter, write_results_to_dir

p = ServerlessSqlParser("dumps/backup.sql", {})   # or {"dialect": "postgres"}
p.initialize()
p.construct()

f = DatabaseFilter()
f.table_rules  = [r".*"]                          # every table
f.column_rules = [r"user", r"pass", r"email"]     # or [r".*"] for every column
results = p.find_credentials(f)

write_results_to_dir(results, "output/backup")    # -> output/backup/<table>.csv per table
print("tables:", list(results))
p.finalize()
```

---

## CLI reference

| Option | Default | Description |
|--------|---------|-------------|
| `--input` | (see `main.py`) | Directory to walk for dump files |
| `--done` | (see `main.py`) | Where successfully processed files are moved |
| `--errors` | (see `main.py`) | Where genuine parse failures are moved |
| `--no-hits` | falls back to `--errors` | Where clean-but-no-match files are moved |
| `--engine` | `serverless` | `serverless` (no server) or `live` (replay into MySQL) |
| `--tables` | all tables | Comma-separated regex for table names (case-insensitive) |
| `--columns` | credential + PII set | Comma-separated regex for column names (case-insensitive) |
| `--null-repr` | `""` (empty) | How a SQL `NULL` is written in the CSV |
| `--dialect` | auto-detect | Force source dialect for serverless (`mysql`, `postgres`, `tsql`, `sqlite`) |

The `--input`/`--done`/`--errors` defaults are placeholder paths in `main.py`; pass
your own on the command line.

---

## Project structure

```text
sql-dedatabaser/
├── main.py                 # CLI entry point and batch pipeline
├── dbparser.py             # base parser, filtering, CSV writers
├── serverless_parser.py    # serverless engine (sqlglot + regex salvage)
├── mysql_parser.py         # live-replay engine (MySQL server)
├── requirements.txt
└── documenation/           # bug / fix tracker
```

---

## Notes

- Scanning is name-based, so review results — a matched column name doesn't guarantee
  the data is sensitive, and an unmatched one could still hold something of interest
  (widen `--columns` if in doubt).
- The tool preserves values as written in the dump; it does not decrypt or unhash
  anything.
