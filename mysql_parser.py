# Attempt get the MySQL connector library. If it's missing store that info for later,
# it's not important to raise an exception now if the user is never going to use the mysql parser

try:
    # noinspection PyUnresolvedReferences
    import mysql.connector.cursor
    # noinspection PyUnresolvedReferences
    from mysql.connector.cursor import CursorBase, MySQLCursor

    has_mysql_connector = True
except ImportError:
    has_mysql_connector = False

try:
    # noinspection PyUnresolvedReferences
    from vericloud import sql
    has_native_sql_support = True
except ImportError:
    has_native_sql_support = False


from dbparser import *


class MySQLParserError(RuntimeError):
    """
    The error type for the MySQL source code parser.
    """

    MISSING_CONNECTOR = 0
    MISSING_NATIVE_SQL_SUPPORT = 1
    PARSER_ALREADY_INITIALIZED = 2
    PARSER_NOT_INITIALIZED = 3
    READER_CREATION_FAILED = 4
    SQL_CONNECTION_FAILED = 5
    CANNOT_ACQUIRE_CURSOR = 6
    EXECUTION_ERROR = 7
    FILTERING_ERROR = 8
    TOO_MANY_ERRORS = 9

    def __init(self, ecode: int, data = None):
        self.args = (ecode, data)

    def __str__(self):
        match self.args[0]:
            case self.MISSING_CONNECTOR:
                return "The module mysql.connector is missing from the environment"
            case self.MISSING_NATIVE_SQL_SUPPORT:
                return "The environment is missing the native SQL parsing module vericloud.sql"
            case self.PARSER_ALREADY_INITIALIZED:
                return "The parser was already initialized and cannot be initialized again"
            case self.PARSER_NOT_INITIALIZED:
                return "The parser was not initialized and this operation cannot be performed"
            case self.READER_CREATION_FAILED:
                return f"The SQL source reader was unable to be created: {self.args[1]}"
            case self.SQL_CONNECTION_FAILED:
                return f"The connection to the host was unable to be performed: {self.args[1]}"
            case self.CANNOT_ACQUIRE_CURSOR:
                return f"Failed to acquire the cursor over the database: {self.args[1]}"
            case self.EXECUTION_ERROR:
                return f"Failed to execute command on line {self.args[1][1]}: {self.args[1][0]}"
            case self.FILTERING_ERROR:
                return f"Error encountered while filtering database: {self.args[1]}"


class MySQLParser(DatabaseParser):
    """
    The MySQL Database Parser.

    MySQL files are usually stored as plain text and are a set of commands to run to rebuild the database on a driver.
    The driver implementations may be different, but they should all function similarly enough that backups should be
    mostly cross-compatible.

    This parser requires that the mysql.connector module is installed: https://dev.mysql.com/doc/connector-python/en/

    This parser also requires a MySQL host, which can either be a remote server or the driver installed on the
    host machine: https://dev.mysql.com/downloads/connector/odbc/

    :raise MySQLParserError: If either the mysql.connector or native SQL support module is missing
    """
    def __init__(self, source_path: str, config: dict):
        super().__init__("MySQL", source_path)

        # Do our checks for invalid environment
        if not has_mysql_connector:
            raise MySQLParserError(MySQLParserError.MISSING_CONNECTOR)
        if not has_native_sql_support:
            raise MySQLParserError(MySQLParserError.MISSING_NATIVE_SQL_SUPPORT)

        # Initialize our attributes
        self.__reader: sql.StatementReader = None
        self.__connection: mysql.connector.CMySQLConnection or None = None
        self.config: dict = config

    def initialize(self) -> bool:
        """
        Initializes the MySQL database parser.

        This method will open the file provided an object instantiation and open a native SQL parser for it.
        It will also attempt to connect to the MySQL host provided via this library's config.

        :raise MySQLParserError: There was an error either creating the file reader or connecting to the database
        server
        :return: True if the initialization was successful, False if the parser had already been initialized
        """
        # Check if we have already initialized
        if self.__reader is not None or self.__connection is not None:
            return False

        # Attempt to create the SQL reader
        try:
            self.__reader = sql.StatementReader(self.source_path)
        except ValueError as e:
            raise MySQLParserError(MySQLParserError.READER_CREATION_FAILED, e) from e

        # Attempt to create the connection to the host
        try:
            self.__connection = mysql.connector.connect(**self.config['connector_cfg'])
        except Exception as e:
            raise MySQLParserError(MySQLParserError.SQL_CONNECTION_FAILED, e) from e

        return True

    def finalize(self):
        """
        Finalized the MySQL database parser.

        This method will not do anything if the object is not initialized. If it is initialized, it releases
        the file parser and the database connection.
        """
        self.__reader = None
        if self.__connection is not None:
            self.__connection.close()
            self.__connection = None

    def find_credentials(
            self,
            db_filter: DatabaseFilter,
            table_validator: Callable[[str], bool] = lambda s: True,
            column_validator: Callable[[str], bool] = lambda s: True
    ) -> FilteredResults:
        """
        Filters the SQL test database with the provided filters.
        :param db_filter: The set of filters to use
        :param table_validator: Validator on table names, can be used to perform more in-depth filtering or acquire
        user input. Defaults to always True
        :param column_validator: Validator on column names, can be used to perform more in-depth filtering or acquire
        user input. Defaults to always True
        :return: The filtered results
        :raises MySQLParserError: If the parser is not initialized, a cursor cannot be acquired, or an error occurs
        while executing filtering statements
        """
        # Check if we are not initialized
        if self.__connection is None or self.__reader is None:
            raise MySQLParserError(MySQLParserError.PARSER_NOT_INITIALIZED)

        # Attempt to acquire the cursor
        try:
            cursor: MySQLCursor = self.__connection.cursor()
        except mysql.connector.ProgrammingError or ValueError as e:
            raise MySQLParserError(MySQLParserError.CANNOT_ACQUIRE_CURSOR, e) from e

        # noinspection PyBroadException
        try:
            # We are initialized so select the test database which we built
            cursor.execute("USE test")

            # Select all the table names that are in the test database
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE TABLE_SCHEMA = n'test'")

            result = FilteredResults()

            # Filter our acquired table names
            # Note: The cursor object supports iteration over the results of the most recent command
            tables = db_filter.filter_tables([name for name, in cursor])

            # Attempt to fetch columns from every table
            for table in tables:
                # Run the table through another validation step
                if not table_validator(table):
                    continue

                # Select every column name that belongs to that table and is in the test database
                cursor.execute(
                    """
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE TABLE_NAME = %s 
                    AND TABLE_SCHEMA = n'test'
                    """,
                    (table, )
                )

                # Filter the received columns
                filtered_columns = db_filter.filter_columns([name for name, in cursor])

                # Filter all the columns again via the user supplied validator
                columns = []
                for column in filtered_columns:
                    if column_validator(column):
                        columns.append(column)

                # If we don't have any columns to fetch then move on to the next table
                if len(columns) == 0:
                    continue

                # Create an empty dictionary in our results for this table
                result[table] = {}

                # Create an empty list for each of our columns
                for column in columns:
                    result[table][column] = []

                # Build a select string from our columns, so that we can acquire
                # all relevant data with one command
                select_str = columns[0]
                if len(columns) > 1:
                    # If we have more than one column, then we need to form a comma separated list
                    for column in columns[1:]:
                        select_str += f", {column}"

                # Finish our selection command to select all the required columns
                select = f"SELECT {select_str} FROM {table}"
                cursor.execute(select)

                # Add all of our results into their respective columns
                # TODO: Find a more efficient way to do this/maybe a one-liner if possible
                for results in cursor:
                    for index, column in enumerate(columns):
                        result[table][column].append(str(results[index]))
            return result
        except Exception as e:
            # We failed to run one of our filtering commands so jump out and let the caller handle it
            raise MySQLParserError(MySQLParserError.FILTERING_ERROR, e) from e

    def construct(self, on_execute: Callable or None = None):
        """
        Rebuilds the SQL database from the file provided during construction.
        :param on_execute: Optional callback for when a statement gets sent to the server
        :raises MySQLParserError: If the parser is not initialized, a cursor cannot be acquired, or an error
        occurs while executing the file
        """
        # Check if the parser is initialized
        if self.__reader is None or self.__connection is None:
            raise MySQLParserError(MySQLParserError.PARSER_NOT_INITIALIZED)

        # Attempt to acquire the cursor
        try:
            cursor: CursorBase = self.__connection.cursor()
        except mysql.connector.ProgrammingError or ValueError as e:
            raise MySQLParserError(MySQLParserError.CANNOT_ACQUIRE_CURSOR, e) from e

        # Set the SQL mode to non-strict, since databases tend to be running on older versions
        # of MySQL which aren't as strict with some types
        cursor.execute("set sql_mode=''")

        # Drop the existing test database if it exists, so we don't have conflicts
        cursor.execute("drop database if exists test")

        # Recreate the test database
        cursor.execute("create database test")

        # Switch to the test database we just created
        cursor.execute("use test")

        error_count = 0
        # The reader provides an interator interface to get each statement from the file as needed
        for command in self.__reader:
            # noinspection PyBroadException
            try:
                # Execute the command parsed from the file
                cursor.execute(command)
            except Exception as e:
                # If we encounter an exception and want to ignore errors, then continue execution
                # but log to the user, otherwise re-raise the exception
                if self.config['ignore_errors'] is True:
                    error_count += 1
                    print(f"SQL Error Ignored on line {self.__reader.line_no()}:{self.__reader.column()}: {e}")
                    if error_count == 500:
                        print("Maximum error count reached. Skipping to next file.")
                        raise MySQLParserError(MySQLParserError.TOO_MANY_ERRORS)
                else:
                    raise MySQLParserError(MySQLParserError.EXECUTION_ERROR, (e, self.__reader.line_no())) from e

            # Run the callback function if it exists
            if on_execute is not None:
                on_execute()


