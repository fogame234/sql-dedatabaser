import sys


def _configure_stdio():
    """
    Force UTF-8 stdout/stderr so the progress bar's Unicode glyphs cannot raise a
    UnicodeEncodeError on a non-UTF-8 Windows console (e.g. cp1252, or a redirected
    pipe), which would otherwise abort processing of an otherwise-fine file.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass


_configure_stdio()

from mysql_parser import *
from serverless_parser import ServerlessSqlParser
from alive_progress import alive_bar
import argparse
import os
from pathlib import Path
import shutil


# Default directories (the original hardcoded paths, kept only as documented defaults).
DEFAULT_INPUT_DIR = r'C:\Users\Trevor\Documents\VeriCloud\Cleanedtest'
DEFAULT_DONE_DIR = r'C:\Users\Trevor\Documents\VeriCloud\Formatting\Done'
DEFAULT_ERROR_DIR = r'C:\Users\Trevor\Documents\VeriCloud\Formatting\Errors'

def _word(token: str) -> str:
    """
    Build a pattern that matches ``token`` only when it is not adjacent to another
    letter — so it stands alone within a delimited name (``user_pin``, ``pin``,
    ``pin-code``). This is used instead of ``\\b`` for short ambiguous tokens because
    ``\\b`` treats ``_`` as a word character (so ``\\bpin\\b`` would miss ``user_pin``),
    while a bare substring would over-match (``pin`` inside ``shipping``). With the
    case-insensitive matching in DatabaseFilter, ``[a-z]`` here covers both cases.
    """
    return r"(?<![a-z])" + token + r"(?![a-z])"


# Column-name patterns worth extracting. The tool's primary focus is credentials
# (usernames/passwords), but any personally identifiable information (PII) is also
# in scope. Matching is case-insensitive and substring-based (see DatabaseFilter),
# so "pass" catches password/passwd/passphrase, "name" catches first_name, etc.
# Short, ambiguous tokens are wrapped with _word() to avoid false positives.
DEFAULT_CREDENTIAL_PATTERNS = [
    r"user", r"pass", r"pwd", r"login", r"cred", r"secret", r"token",
    r"auth", r"session", _word("pin"), r"hash", r"salt", r"api[_-]?key", r"otp",
]
DEFAULT_PII_PATTERNS = [
    # identity / contact
    r"email", _word("mail"), r"name", r"phone", r"mobile", _word("cell"), _word("fax"),
    r"addr", r"street", _word("city"), _word("state"), r"zip", r"postal", r"country",
    # government / national IDs
    r"ssn", r"social", r"national", r"passport", r"licen", r"driver", _word("tax"), _word("tin"),
    # financial
    r"card", r"credit", r"debit", r"cvv", r"cvc", r"iban", r"swift",
    r"account", _word("acct"), r"routing", _word("bank"),
    # demographic
    _word("dob"), r"birth", r"gender", _word("sex"),
]
DEFAULT_COLUMN_RULES = DEFAULT_CREDENTIAL_PATTERNS + DEFAULT_PII_PATTERNS


def parse_args():
    parser = argparse.ArgumentParser(
        description="Reconstruct MySQL dump files and extract filtered tables/columns to CSV."
    )
    parser.add_argument('--input', default=DEFAULT_INPUT_DIR,
                        help="Directory to walk for dump files")
    parser.add_argument('--done', default=DEFAULT_DONE_DIR,
                        help="Directory to move successfully processed files into")
    parser.add_argument('--errors', default=DEFAULT_ERROR_DIR,
                        help="Directory to move failed files into")
    parser.add_argument('--engine', choices=('serverless', 'live'), default='serverless',
                        help="serverless: parse dumps directly, no DB server needed (default). "
                             "live: replay each dump into a MySQL server, then read it back.")
    parser.add_argument('--null-repr', default='',
                        help="How a SQL NULL is written in the CSV (default: empty field)")
    parser.add_argument('--dialect', default=None,
                        help="Force a source SQL dialect for the serverless engine "
                             "(e.g. mysql, postgres, tsql, sqlite) instead of auto-detecting")
    parser.add_argument('--tables', default=None,
                        help="Comma-separated regex patterns for which table names to extract "
                             "(default: all tables). Matching is case-insensitive.")
    parser.add_argument('--columns', default=None,
                        help="Comma-separated regex patterns for which column names to extract "
                             "(default: the built-in credential + PII patterns). "
                             "Matching is case-insensitive.")
    parser.add_argument('--no-hits', default=None,
                        help="Directory for files that parsed cleanly but matched no "
                             "credential/PII columns. Keeps them separate from genuine parse "
                             "failures. If omitted, such files go to the --errors dir.")
    return parser.parse_args()


def _split_patterns(value: str):
    """Parse a comma-separated pattern string into a list, or None if not given."""
    if value is None:
        return None
    return [p.strip() for p in value.split(',') if p.strip()]


def make_parser(engine: str, src_path: str, config: dict):
    """Construct the requested parser. Both implement the DatabaseParser interface."""
    if engine == 'live':
        return MySQLParser(src_path, config)
    return ServerlessSqlParser(src_path, config)


def process_file(root, fn, engine, config, db_filter, done_dir, error_dir, no_hits_dir, null_repr):
    """Process a single dump file. Returns nothing; routes the file to done/no-hits/error."""
    src_path = None
    csv_dir_path = None
    try:
        # Create a Path object for the source file and the CSV output directory
        src_path = Path(root).joinpath(fn)
        stem, ext = os.path.splitext(fn)
        csv_dir_path = Path(root).joinpath(stem)
        if csv_dir_path.exists():
            return
        csv_dir_path.mkdir(parents=True, exist_ok=True)

        # Parse the file with the chosen parser
        parser = make_parser(engine, str(src_path), config)
        parser.initialize()
        verb = "Reconstructing" if engine == 'live' else "Parsing"
        with alive_bar(title=f"{verb} Database: {stem}.sql") as bar:
            parser.construct(bar)

        # Look for credentials in the parsed file that match a certain filter
        results = parser.find_credentials(db_filter)
        parser.finalize()

        if not results:
            # The file parsed fine but matched no credential/PII columns. This is not a
            # failure, so route it to the no-hits dir (falling back to the error dir if
            # none was configured), and drop the empty working directory.
            dest = no_hits_dir if no_hits_dir else error_dir
            label = "no-hits" if no_hits_dir else "errors"
            print(f"No matching columns in {fn}: moved to {label} dir")
            shutil.move(str(src_path), os.path.join(dest, fn))
            shutil.rmtree(csv_dir_path, ignore_errors=True)
            return

        # Write one CSV per extracted table into the dump's own folder, then archive
        # the original dump alongside them.
        write_results_to_dir(results, str(csv_dir_path), null_repr)
        shutil.move(str(src_path), str(csv_dir_path))

        # Move the completed working directory into the done dir
        try:
            shutil.move(str(csv_dir_path), done_dir)
        except (shutil.Error, OSError) as e:
            print(f"Could not move {csv_dir_path} to done dir: {e}")

    # A parser error (too many SQL errors, unreadable file, etc.): log it, move the
    # file to the error dir, and carry on so one bad file cannot stop the whole run.
    except MySQLParserError as e:
        if e.args[0] == MySQLParserError.TOO_MANY_ERRORS:
            print(f"Too many errors in file: {fn}")
        else:
            print(f"Parser error on {fn}: {e}")
        _route_to_error(src_path, fn, csv_dir_path, error_dir)
    except Exception as e:
        print(f"Unexpected error on {fn}: {e}")
        _route_to_error(src_path, fn, csv_dir_path, error_dir)


def _route_to_error(src_path, fn, csv_dir_path, error_dir):
    """Best-effort move of a failed file to the error dir and cleanup of its work dir."""
    try:
        if src_path is not None and os.path.exists(src_path):
            shutil.move(str(src_path), os.path.join(error_dir, fn))
    except (shutil.Error, OSError) as e:
        print(f"Could not move {fn} to error dir: {e}")
    if csv_dir_path is not None:
        shutil.rmtree(csv_dir_path, ignore_errors=True)


def main():
    args = parse_args()

    config = {
        'connector_cfg': {
            'user': 'root',
            'password': 'password',
            'host': 'localhost',
            'use_pure': True
        },
        'ignore_errors': True,
        'dialect': args.dialect,
    }

    db_filter = DatabaseFilter()
    # Scan all tables by default (credentials/PII can live in any table), and extract
    # the credential + PII columns. Either can be overridden on the command line.
    db_filter.table_rules = _split_patterns(args.tables) or [r".*"]
    db_filter.column_rules = _split_patterns(args.columns) or DEFAULT_COLUMN_RULES

    # Make sure the output directories exist so a move can't fail on a missing dir
    for out_dir in (args.done, args.errors, args.no_hits):
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

    for root, dirs, files in os.walk(args.input):
        for fn in files:
            process_file(root, fn, args.engine, config, db_filter,
                         args.done, args.errors, args.no_hits, args.null_repr)


if __name__ == '__main__':
    main()
