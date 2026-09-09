"""
Serverless SQL dump parser.

Extracts tables and rows directly from a SQL dump file without needing a live
database server. It is deliberately tolerant of non-standard / malformed files and
works in two tiers per statement:

- Tier C (structural): parse the statement with ``sqlglot`` (when installed) and
  read CREATE TABLE / INSERT nodes straight out of the AST. Handles many dialects
  (MySQL, PostgreSQL, MSSQL, SQLite, ...).
- Tier D (salvage): when sqlglot is unavailable or cannot parse a statement, fall
  back to a tolerant regex/scanner that recovers rows from INSERT statements even
  when the surrounding file will not parse as a whole.

Both tiers are fed by a robust, dependency-free statement splitter that respects
string literals, quoting styles, comments, and ``DELIMITER`` directives, so an
embedded ``;`` never mis-splits a statement.

This parser implements the same :class:`DatabaseParser` interface as the live
``MySQLParser``, so it drops into the same pipeline as an alternative that needs no
server, credentials, or network.
"""

import os
import re

# sqlglot powers the structural (Tier C) parse. It is optional: without it the
# parser still recovers data through the regex salvage tier (Tier D).
try:
    import sqlglot
    from sqlglot import expressions as exp

    _HAS_SQLGLOT = True
except ImportError:
    _HAS_SQLGLOT = False

# charset-normalizer improves encoding detection for non-UTF-8 dumps. Optional.
try:
    import charset_normalizer

    _HAS_CHARSET = True
except ImportError:
    _HAS_CHARSET = False


from dbparser import DatabaseParser, DatabaseFilter, FilteredResults, TableData
from typing import Callable


class ServerlessParserError(RuntimeError):
    """Raised for unrecoverable errors in the serverless parser (e.g. missing file)."""


# ---------------------------------------------------------------------------
# Encoding + dialect detection
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    """Read a dump file as text, detecting the encoding and stripping any BOM."""
    with open(path, 'rb') as handle:
        raw = handle.read()

    if _HAS_CHARSET:
        best = charset_normalizer.from_bytes(raw).best()
        if best is not None:
            return str(best)

    # Fallback chain when charset-normalizer is unavailable
    for encoding in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _detect_dialect(text: str) -> str:
    """
    Best-effort fingerprint of the source SQL dialect, used as sqlglot's ``read``
    dialect. Defaults to MySQL, which is also the most permissive for our purposes.
    """
    head = text[:20000]
    lower = head.lower()

    if 'engine=' in lower or '/*!' in head or '`' in head:
        return 'mysql'
    if 'from stdin' in lower or '\\.' in head or '::' in head:
        return 'postgres'
    if re.search(r'(?im)^\s*go\s*$', head) or 'identity(' in lower:
        return 'tsql'
    if 'pragma ' in lower or 'autoincrement' in lower:
        return 'sqlite'
    return 'mysql'


# ---------------------------------------------------------------------------
# Statement splitting (dependency-free, dialect-agnostic)
# ---------------------------------------------------------------------------

def _iter_statements(text: str):
    """
    Yield SQL statements one at a time from raw dump text.

    Respects single/double-quoted strings (with ``''`` and ``\\`` escapes), backtick
    identifiers, ``--``/``#`` line comments, ``/* */`` block comments, and
    ``DELIMITER`` directives. An embedded terminator inside any of those never ends
    a statement. Never raises, so a malformed region cannot abort the whole file.
    """
    delimiter = ';'
    n = len(text)
    i = 0
    buf = []
    seen_nonspace = False
    in_sq = in_dq = in_bt = False
    in_line_comment = in_block_comment = False

    def flush():
        nonlocal buf, seen_nonspace
        stmt = ''.join(buf).strip()
        buf = []
        seen_nonspace = False
        return stmt if stmt else None

    while i < n:
        c = text[i]

        if in_line_comment:
            buf.append(c)
            if c == '\n':
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(c)
            if c == '*' and i + 1 < n and text[i + 1] == '/':
                buf.append('/')
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if in_sq:
            buf.append(c)
            if c == '\\' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_sq = False
            i += 1
            continue
        if in_dq:
            buf.append(c)
            if c == '\\' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_dq = False
            i += 1
            continue
        if in_bt:
            buf.append(c)
            if c == '`':
                if i + 1 < n and text[i + 1] == '`':
                    buf.append('`')
                    i += 2
                    continue
                in_bt = False
            i += 1
            continue

        # DELIMITER directive, only at the start of a statement
        if not seen_nonspace and text[i:i + 9].upper() == 'DELIMITER' and (
                i + 9 >= n or text[i + 9] in ' \t'):
            eol = text.find('\n', i)
            if eol == -1:
                eol = n
            new_delim = text[i + 9:eol].strip()
            if new_delim:
                delimiter = new_delim
            buf = []
            seen_nonspace = False
            i = eol + 1
            continue

        # Comment openers
        if c == '-' and i + 1 < n and text[i + 1] == '-' and (i + 2 >= n or text[i + 2] in ' \t\r\n'):
            in_line_comment = True
            buf.append(c)
            i += 1
            continue
        if c == '#':
            in_line_comment = True
            buf.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            in_block_comment = True
            buf.append(c)
            buf.append('*')
            i += 2
            continue

        # String / identifier openers
        if c == "'":
            in_sq = True
            seen_nonspace = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_dq = True
            seen_nonspace = True
            buf.append(c)
            i += 1
            continue
        if c == '`':
            in_bt = True
            seen_nonspace = True
            buf.append(c)
            i += 1
            continue

        # Statement terminator
        if c == delimiter[0] and text[i:i + len(delimiter)] == delimiter:
            stmt = flush()
            if stmt:
                yield stmt
            i += len(delimiter)
            continue

        buf.append(c)
        if not c.isspace():
            seen_nonspace = True
        i += 1

    tail = flush()
    if tail:
        yield tail


def _strip_leading_comments(stmt: str) -> str:
    """
    Remove any leading line/block comments and whitespace from a statement, so the
    real leading keyword is exposed for dispatch and the salvage regexes' ``^``
    anchors match the actual SQL (a dump often prefixes statements with a comment).
    """
    prev = None
    s = stmt
    while prev != s:
        prev = s
        s = re.sub(r'^\s*--[^\n]*', '', s)
        s = re.sub(r'^\s*#[^\n]*', '', s)
        s = re.sub(r'^\s*/\*.*?\*/', '', s, flags=re.DOTALL)
        s = s.lstrip()
    return s


def _leading_keyword(stmt: str) -> str:
    """Return the uppercased leading SQL keyword of a statement (e.g. ``INSERT``)."""
    match = re.match(r'\s*([A-Za-z_]+)', stmt)
    return match.group(1).upper() if match else ''


# ---------------------------------------------------------------------------
# Identifier / value helpers (shared by the salvage tier)
# ---------------------------------------------------------------------------

_QUOTE_CLOSERS = {'`': '`', '"': '"', '[': ']'}


def _strip_quotes(token: str) -> str:
    """Strip one layer of surrounding ``backtick`` / ``"`` / ``[]`` quotes."""
    token = token.strip()
    if len(token) >= 2 and token[0] in _QUOTE_CLOSERS and token[-1] == _QUOTE_CLOSERS[token[0]]:
        return token[1:-1]
    return token


def _split_qualified(raw: str) -> list[str]:
    """Split ``db.schema.table`` on top-level dots, respecting quotes/brackets."""
    parts = []
    cur = []
    closer = None
    for ch in raw:
        if closer is not None:
            cur.append(ch)
            if ch == closer:
                closer = None
            continue
        if ch in _QUOTE_CLOSERS:
            closer = _QUOTE_CLOSERS[ch]
            cur.append(ch)
            continue
        if ch == '.':
            parts.append(''.join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append(''.join(cur))
    return parts


def _canon_table(raw: str) -> str:
    """Canonical, unquoted table name (last segment of any qualified name)."""
    parts = _split_qualified(raw.strip())
    return _strip_quotes(parts[-1] if parts else raw)


def _first_identifier(segment: str):
    """Return the first (unquoted) identifier in a column-definition segment."""
    segment = segment.lstrip()
    if not segment:
        return None
    c = segment[0]
    if c in _QUOTE_CLOSERS:
        close = _QUOTE_CLOSERS[c]
        out = []
        j = 1
        while j < len(segment):
            ch = segment[j]
            if ch == close:
                if c != '[' and j + 1 < len(segment) and segment[j + 1] == close:
                    out.append(close)
                    j += 2
                    continue
                break
            out.append(ch)
            j += 1
        return ''.join(out)
    match = re.match(r'[\w$]+', segment)
    return match.group(0) if match else None


def _split_top_level_commas(s: str) -> list[str]:
    """Split on top-level commas, respecting quotes and nested parentheses."""
    parts = []
    cur = []
    depth = 0
    in_sq = in_dq = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_sq:
            cur.append(ch)
            if ch == '\\' and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if ch == "'":
                if i + 1 < n and s[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                in_sq = False
            i += 1
            continue
        if in_dq:
            cur.append(ch)
            if ch == '\\' and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if ch == '"':
                if i + 1 < n and s[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dq = True
            cur.append(ch)
            i += 1
            continue
        if ch == '(':
            depth += 1
            cur.append(ch)
            i += 1
            continue
        if ch == ')':
            depth -= 1
            cur.append(ch)
            i += 1
            continue
        if ch == ',' and depth == 0:
            parts.append(''.join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append(''.join(cur))
    return parts


def _iter_value_tuples(s: str):
    """Yield the inner text of each top-level ``(...)`` group in a VALUES list."""
    depth = 0
    in_sq = in_dq = False
    cur = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_sq:
            cur.append(ch)
            if ch == '\\' and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if ch == "'":
                if i + 1 < n and s[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                in_sq = False
            i += 1
            continue
        if in_dq:
            cur.append(ch)
            if ch == '\\' and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if ch == '"':
                if i + 1 < n and s[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dq = True
            cur.append(ch)
            i += 1
            continue
        if ch == '(':
            depth += 1
            if depth == 1:
                cur = []
                i += 1
                continue
            cur.append(ch)
            i += 1
            continue
        if ch == ')':
            depth -= 1
            if depth == 0:
                yield ''.join(cur)
                cur = []
                i += 1
                continue
            cur.append(ch)
            i += 1
            continue
        if depth >= 1:
            cur.append(ch)
        i += 1


_ESCAPE_MAP = {'n': '\n', 't': '\t', 'r': '\r', '0': '\0', 'b': '\b', 'Z': '\x1a',
               '\\': '\\', "'": "'", '"': '"', '`': '`'}


def _unquote_string(token: str) -> str:
    """Unquote a SQL string literal, resolving ``''`` and backslash escapes."""
    quote = token[0]
    body = token[1:-1] if len(token) >= 2 and token[-1] == quote else token[1:]
    out = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == '\\' and i + 1 < n:
            out.append(_ESCAPE_MAP.get(body[i + 1], body[i + 1]))
            i += 2
            continue
        if ch == quote and i + 1 < n and body[i + 1] == quote:
            out.append(quote)
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _parse_value_token(token: str):
    """Convert one raw VALUES token to a Python value (``None`` for NULL)."""
    t = token.strip()
    if t == '':
        return None
    upper = t.upper()
    if upper == 'NULL':
        return None
    if upper == 'TRUE':
        return True
    if upper == 'FALSE':
        return False
    if t[0] in ("'", '"'):
        return _unquote_string(t)
    # Numbers, hex literals, function calls, expressions: keep the raw text
    return t


# ---------------------------------------------------------------------------
# Salvage-tier (regex) extraction
# ---------------------------------------------------------------------------

_IDENT = r'`[^`]+`|"[^"]+"|\[[^\]]+\]|[\w$.]+'

_CREATE_RE = re.compile(
    r'^\s*CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?P<name>' + _IDENT + r')\s*\((?P<body>.*)\)[^)]*$',
    re.IGNORECASE | re.DOTALL,
)

_INSERT_RE = re.compile(
    r'^\s*(?:INSERT|REPLACE)\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE)\s+)*'
    r'(?:INTO\s+)?(?P<name>' + _IDENT + r')'
    r'\s*(?:\((?P<cols>[^)]*)\))?\s*VALUES?\s*(?P<vals>.*)$',
    re.IGNORECASE | re.DOTALL,
)

_CONSTRAINT_KW = {'PRIMARY', 'UNIQUE', 'KEY', 'INDEX', 'CONSTRAINT', 'FOREIGN',
                  'CHECK', 'FULLTEXT', 'SPATIAL'}


def _regex_create(stmt: str):
    """Extract ``(table_name, [columns])`` from a CREATE TABLE via regex."""
    match = _CREATE_RE.match(stmt)
    if not match:
        return None
    name = _canon_table(match.group('name'))
    columns = []
    for segment in _split_top_level_commas(match.group('body')):
        first = _first_identifier(segment)
        if first is None or first.upper() in _CONSTRAINT_KW:
            continue
        columns.append(first)
    return name, columns


def _regex_insert(stmt: str):
    """Extract ``(table_name, columns_or_None, rows)`` from an INSERT via regex."""
    match = _INSERT_RE.match(stmt)
    if not match:
        return None
    name = _canon_table(match.group('name'))

    columns = None
    if match.group('cols'):
        columns = [c for c in (_first_identifier(p) for p in _split_top_level_commas(match.group('cols'))) if c]

    rows = []
    for tup in _iter_value_tuples(match.group('vals')):
        rows.append([_parse_value_token(tok) for tok in _split_top_level_commas(tup)])

    if not rows:
        return None
    return name, columns, rows


# ---------------------------------------------------------------------------
# Structural-tier (sqlglot) extraction
# ---------------------------------------------------------------------------

def _cell_value(cell):
    """Convert one sqlglot value node to a Python value (``None`` for NULL)."""
    if isinstance(cell, exp.Null):
        return None
    if isinstance(cell, exp.Boolean):
        return bool(cell.this)
    if isinstance(cell, exp.Literal):
        # Keep numbers as their exact source text to avoid float rounding
        return cell.name
    # Negative numbers, hex, functions (NOW(), etc.): keep the rendered SQL text
    return cell.sql()


def _sqlglot_create(expr):
    table = expr.find(exp.Table)
    if table is None:
        return None
    columns = [c.name for c in expr.find_all(exp.ColumnDef)]
    return table.name, columns


def _sqlglot_insert(expr):
    this = expr.this
    if isinstance(this, exp.Schema):
        table = this.this
        name = table.name if table is not None else None
        columns = [c.name for c in this.expressions]
    elif isinstance(this, exp.Table):
        name = this.name
        columns = None
    else:
        name = getattr(this, 'name', None)
        columns = None

    if not name:
        return None

    values = expr.expression
    if not isinstance(values, exp.Values):
        # INSERT ... SELECT and similar carry no literal rows to extract
        return None

    rows = []
    for tup in values.expressions:
        cells = tup.expressions if hasattr(tup, 'expressions') else [tup]
        rows.append([_cell_value(cell) for cell in cells])
    return name, columns, rows


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

class ServerlessSqlParser(DatabaseParser):
    """
    Extracts tables and rows from a SQL dump file without a database server.

    Implements the same interface as the live :class:`MySQLParser`: ``initialize``,
    ``construct`` (parses the file into memory), ``find_credentials`` (filters and
    returns results), and ``finalize``.

    :param source_path: Path to the SQL dump file
    :param config: Optional settings: ``dialect`` (force a sqlglot read dialect
        instead of auto-detecting), and any other keys are ignored
    """

    def __init__(self, source_path: str, config: dict = None):
        super().__init__("MySQL (serverless)", source_path)
        self.config = config or {}
        # table name -> {"columns": [...], "index": {name: idx}, "rows": [[...]]}
        self._tables: dict = {}
        self._initialized = False
        self.stats = {"statements": 0, "sqlglot": 0, "regex": 0, "skipped": 0}

    def initialize(self) -> bool:
        if self._initialized:
            return False
        if not os.path.isfile(self.source_path):
            raise ServerlessParserError(f"source file not found: {self.source_path}")
        self._initialized = True
        return True

    def finalize(self):
        self._tables = {}
        self._initialized = False

    def construct(self, on_execute: Callable = None):
        """Parse the dump file into in-memory tables. Calls ``on_execute`` per statement."""
        if not self._initialized:
            raise ServerlessParserError("parser not initialized")

        text = _read_text(self.source_path)
        dialect = self.config.get('dialect') or _detect_dialect(text)

        for stmt in _iter_statements(text):
            self.stats["statements"] += 1
            self._ingest_statement(stmt, dialect)
            if on_execute is not None:
                on_execute()

    def find_credentials(
            self,
            db_filter: DatabaseFilter,
            table_validator: Callable[[str], bool] = lambda s: True,
            column_validator: Callable[[str], bool] = lambda s: True,
    ) -> FilteredResults:
        if not self._initialized:
            raise ServerlessParserError("parser not initialized")

        result: FilteredResults = {}
        for table in db_filter.filter_tables(list(self._tables.keys())):
            if not table_validator(table):
                continue
            tbl = self._tables[table]

            filtered = db_filter.filter_columns(tbl["columns"])
            columns = [c for c in filtered if column_validator(c)]
            if not columns:
                continue

            indices = [tbl["index"][c] for c in columns]
            table_data = TableData(columns)
            for row in tbl["rows"]:
                table_data.add_row([row[i] if i < len(row) else None for i in indices])
            result[table] = table_data
        return result

    # -- internal ----------------------------------------------------------

    def _ingest_statement(self, stmt: str, dialect: str):
        # Expose the real keyword even when the statement is prefixed by a comment
        stmt = _strip_leading_comments(stmt)
        keyword = _leading_keyword(stmt)
        if keyword not in ('CREATE', 'INSERT', 'REPLACE'):
            self.stats["skipped"] += 1
            return

        # Tier C: structural parse via sqlglot
        if _HAS_SQLGLOT:
            try:
                expr = sqlglot.parse_one(stmt, read=dialect)
            except Exception:
                expr = None
            if expr is not None:
                if isinstance(expr, exp.Create):
                    got = _sqlglot_create(expr)
                    if got:
                        self._register_columns(*got)
                        self.stats["sqlglot"] += 1
                        return
                elif isinstance(expr, exp.Insert):
                    got = _sqlglot_insert(expr)
                    if got:
                        self._add_rows(*got)
                        self.stats["sqlglot"] += 1
                        return

        # Tier D: regex salvage
        if keyword == 'CREATE':
            got = _regex_create(stmt)
            if got and got[1]:
                self._register_columns(*got)
                self.stats["regex"] += 1
                return
        else:
            got = _regex_insert(stmt)
            if got:
                self._add_rows(*got)
                self.stats["regex"] += 1
                return

        self.stats["skipped"] += 1

    def _new_table(self) -> dict:
        return {"columns": [], "index": {}, "rows": []}

    def _add_column(self, tbl: dict, name: str):
        """Append a new column and backfill existing rows with NULL for it."""
        if name in tbl["index"]:
            return
        tbl["index"][name] = len(tbl["columns"])
        tbl["columns"].append(name)
        for row in tbl["rows"]:
            row.append(None)

    def _register_columns(self, name: str, columns: list):
        """Record the column set/order for a table (from a CREATE TABLE)."""
        if not columns:
            return
        tbl = self._tables.setdefault(name, self._new_table())
        for column in columns:
            self._add_column(tbl, column)

    def _add_rows(self, name: str, columns, rows: list):
        """Append rows to a table, aligning them to its established columns."""
        tbl = self._tables.setdefault(name, self._new_table())

        if columns is None:
            # Positional insert: establish/extend columns to the widest row
            width = max((len(r) for r in rows), default=0)
            while len(tbl["columns"]) < width:
                self._add_column(tbl, f"col{len(tbl['columns']) + 1}")
            for row in rows:
                full = [None] * len(tbl["columns"])
                for idx, value in enumerate(row):
                    if idx < len(full):
                        full[idx] = value
                tbl["rows"].append(full)
            return

        # Named insert: make sure every named column exists, then map by name
        for column in columns:
            self._add_column(tbl, column)
        for row in rows:
            full = [None] * len(tbl["columns"])
            for idx, column in enumerate(columns):
                if idx < len(row):
                    full[tbl["index"][column]] = row[idx]
            tbl["rows"].append(full)
