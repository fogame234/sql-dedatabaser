import csv
import os
import re
from typing import Any, Callable


class TableData:
    """
    Row-major container for a single table's extracted data.

    Storing rows (rather than the previous column-major dict of lists) keeps each
    record intact: rows can never silently misalign when columns differ in length,
    duplicate column names survive, and a SQL NULL is preserved as Python ``None``
    instead of being flattened into the literal string ``"None"``.
    """

    def __init__(self, columns: list[str]):
        self.columns: list[str] = list(columns)
        self.rows: list[list[Any]] = []

    def add_row(self, values):
        """Append one record. Cells are stored as raw values (``None`` == SQL NULL)."""
        self.rows.append(list(values))


# A mapping of table name -> its extracted data.
FilteredResults = dict[str, TableData]


def _format_cell(value: Any, null_repr: str = "") -> str:
    """
    Serializes a single cell value to the string form written into the CSV.

    :param value: The raw cell value (``None`` for a SQL NULL)
    :param null_repr: What to emit for a NULL (default: empty field, which is
        distinct from the literal string ``"None"`` or ``"NULL"``)
    :return: The CSV field text
    """
    if value is None:
        return null_repr
    if isinstance(value, bool):
        # Match MySQL's tinyint(1) representation rather than Python's "True"/"False"
        return "1" if value else "0"
    if isinstance(value, (bytes, bytearray)):
        # Preserve binary/BLOB data losslessly: decode as UTF-8 text when possible,
        # otherwise fall back to a hex representation instead of a Python repr.
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + bytes(value).hex()
    return str(value)


def _write_result_table_to_file(results: FilteredResults, table: str, path: str,
                                modify_filename: bool, null_repr: str = ""):
    """
    Handles writing a single table from database filtering results to a file.
    :param results: The database filtered results in its entirety
    :param table: The name of the table to serialize
    :param path: The filepath to serialize to (unmodified from user-provided input)
    :param modify_filename: If the file stem should be appended with the table name
    :param null_repr: The CSV representation of a SQL NULL
    """
    table_data = results[table]
    if modify_filename:
        stem, ext = os.path.splitext(path)
        path = f"{stem}_{table}{ext}"
    # Guard against a table with no columns
    if not table_data.columns:
        return
    # newline="" is required so the csv module's own line terminator is not doubled
    # on Windows (which would otherwise insert a blank line between every row).
    with open(path, 'w', newline='', encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(table_data.columns)
        for row in table_data.rows:
            writer.writerow([_format_cell(value, null_repr) for value in row])


def write_results_to_file(results: FilteredResults, path: str, null_repr: str = ""):
    """
    Writes the results of a database filtering to a file(s) in CSV format.

    Note: If the provided filtering results contains multiple tables, multiple files will be saved in the
    pattern of 'filename_tablename.extension'
    :param results: The results from a database filtering operation
    :param path: The destination filepath
    :param null_repr: The CSV representation of a SQL NULL
    """
    # Get the keys as a list, since you can't index into the dict_keys type
    table_names = [key for key in results.keys()]

    # If there is only one table, then we are going to export that file exactly as provided
    if len(table_names) == 1:
        _write_result_table_to_file(results, table_names[0], path, False, null_repr)
    else:
        # There are multiple tables, so we are going to save numerous files
        for table_name in table_names:
            _write_result_table_to_file(results, table_name, path, True, null_repr)


def _safe_filename(name: str) -> str:
    """Turn a table name into a filesystem-safe file stem."""
    safe = re.sub(r'[^\w.\-]+', '_', name).strip('_. ')
    return safe or 'table'


def write_results_to_dir(results: FilteredResults, out_dir: str, null_repr: str = ""):
    """
    Writes filtering results into ``out_dir`` as one CSV per table, each named after
    the table (``<out_dir>/<table>.csv``). The directory is created if needed.

    This is the folder-per-dump output: point ``out_dir`` at a folder named after the
    source ``.sql`` file, and every extracted table lands inside it.

    :param results: The results from a database filtering operation
    :param out_dir: The destination directory
    :param null_repr: The CSV representation of a SQL NULL
    """
    os.makedirs(out_dir, exist_ok=True)
    used = set()
    for table_name in results.keys():
        base = _safe_filename(table_name)
        # Avoid collisions when two table names sanitize to the same stem
        stem = base
        counter = 2
        while stem in used:
            stem = f"{base}_{counter}"
            counter += 1
        used.add(stem)
        path = os.path.join(out_dir, f"{stem}.csv")
        _write_result_table_to_file(results, table_name, path, False, null_repr)


class DatabaseFilter:
    """
    Helper structure to manage filtering rules for databases.

    Note: It is recommended to use fewer patterns with better success rates if possible, as every table/column name
    is matched against every filtering rule.
    """
    def __init__(self):
        self.table_rules: list[str] = []
        """
        A list of regex patterns to match table names against. Any successful match is used.
        """

        self.column_rules: list[str] = []
        """
        A list of regex patterns to match column names against. Any successful match is used.
        """

    @staticmethod
    def _apply_filters(items: list[str], filters: list[str]) -> list[str]:
        """
        Applies the provided filters to the items and returns a new list of filtered names
        :param items: The items to filter
        :param filters: The regex patterns to match against
        :return: The filtered list
        """
        # Create our empty list
        output: list[str] = []

        # Check every item against every pattern. re.search (not re.match) so a rule
        # matches anywhere in the name unless the user anchors it explicitly with ^;
        # matching is case-insensitive since column/table names vary in casing
        # (Email, SSN, FirstName) but the intent is the same.
        for item in items:
            for rule in filters:
                if re.search(rule, item, re.IGNORECASE):
                    output.append(item)
                    break
        return output

    def filter_tables(self, tables: list[str]) -> list[str]:
        """
        Filters a list of tables against the preset list of filters
        :param tables: The tables to filter
        :return: The filtered tables
        """
        return self._apply_filters(tables, self.table_rules)

    def filter_columns(self, columns: list[str]) -> list[str]:
        """
        Filters a list of columns against the preset list of filters
        :param columns: The columns to filter
        :return: The filtered columns
        """
        return self._apply_filters(columns, self.column_rules)


class DatabaseParser:
    """
    The base class for all database parsers.

    When parsing information, this class should function as a stream, meaning that it should not read all the data
    at once, nor should it store all fo the data in memory over time. It should read the data as it needs and export
    the data as requested.
    """

    def __init__(self, ty: str, source_path: str):
        """
        :param ty: The type of database
        :param source_path: The path to the file which contains source information.
        """
        self.ty = ty
        self.source_path = source_path

    def initialize(self) -> bool:
        """
        Initializes the database parser.

        This method should not perform any of the data parsing, but should initialize the parser
        such that any requests after this call are able to return the requested data.

        :raise NotImplementedError: Base class implementation raises this immediately
        :return: True if the initialization was successful, False if the parser had already been initialized
        """
        raise NotImplementedError

    def finalize(self):
        """
        Finalizes the database parser.

        This method should not perform any remaining data parsing if unnecessary, but should close the parser and all
        opened resources cleanly.

        Any data requests after the call to this function cannot be assumed to be safe/proper.
        :raise NotImplementedError: Base class implementation raises this immediately
        """
        raise NotImplementedError

    def find_credentials(
            self,
            db_filter: DatabaseFilter,
            table_validator: Callable[[str], bool] = lambda s: True,
            column_validator: Callable[[str], bool] = lambda s: True
    ) -> FilteredResults:
        """
        Applies the provided filters to the database and returns a list of results, which can then
        be serialized.
        :param db_filter: The set of filters to use
        :param table_validator: Validator on table names, can be used to perform more in-depth filtering or acquire
        user input
        :param column_validator: Validator on column names, can be used to perform more in-depth filtering or acquire
        user input
        :return: The filtered results
        :raises NotImplementedError: Base class implementation raises this immediately
        """
        raise NotImplementedError
