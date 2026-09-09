import csv
import os
import re
from typing import Callable

FilteredResults = dict[str, dict[str, list[str]]]


def _write_result_table_to_file(results: FilteredResults, table: str, path: str, modify_filename: bool):
    """
    Handles writing a single table from database filtering results to a file.
    :param results: The database filtered results in its entirety
    :param table: The name of the table to serialize
    :param path: The filepath to serialize to (unmodified from user-provided input)
    :param modify_filename: If the file stem should be appended with the table name
    """
    table_dict = results[table]
    if modify_filename:
        stem, ext = os.path.splitext(path)
        path = f"{stem}_{table}{ext}"
    with open(path, 'w', encoding="utf-8") as csv_file:
        columns = [key for key in table_dict.keys()]
        writer = csv.DictWriter(csv_file, fieldnames = columns)
        writer.writeheader()
        row_count = len(table_dict[columns[0]])
        for row in range(0, row_count):
            row_fields = {}
            for column in columns:
                row_fields[column] = table_dict[column][row]
            writer.writerow(row_fields)


def write_results_to_file(results: FilteredResults, path: str):
    """
    Writes the results of a database filtering to a file(s) in CSV format.

    Note: If the provided filtering results contains multiple tables, multiple files will be saved in the
    pattern of 'filename_tablename.extension'
    :param results: The results from a database filtering operation
    :param path: The destination filepath
    """
    # Get the keys as a list, since you can't index into the dict_keys type
    table_names = [key for key in results.keys()]

    # If there is only one table, then we are going to export that file exactly as provided
    if len(table_names) == 1:
        _write_result_table_to_file(results, table_names[0], path, False)
    else:
        # There are multiple tables, so we are going to save numerous files
        for table_name in table_names:
            _write_result_table_to_file(results, table_name, path, True)


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

        # Check every item against every pattern
        for item in items:
            for rule in filters:
                if re.match(rule, item):
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
            table_validator: Callable[[str], bool],
            column_validator: Callable[[str], bool]
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
