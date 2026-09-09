from mysql_parser import *
from alive_progress import alive_bar
import os
from pathlib import Path
import shutil

if __name__ == '__main__':
    # noinspection PyBroadException
    try:
        use_progress_bar = True
    except Exception:
        use_progress_bar = False

    config = {
        'connector_cfg': {
            'user': 'root',
            'password': 'password',
            'host': 'localhost',
            'use_pure': True
        },
        'ignore_errors': True
    }

    db_filter = DatabaseFilter()
    db_filter.table_rules = [r".*user.*"]
    db_filter.column_rules = [r".*"]
    # db_filter.column_rules = [
    #     r".*user.*", r".*pass.*", r".*login.*", r".*email.*",
    #     r".*name.*", r".*pin.*", r".*token.*"
    # ]

    error_dir = r'C:\Users\Trevor\Documents\Vericloud\Formatting\Errors'
    done_dir = r'C:\Users\Trevor\Documents\Vericloud\Formatting\Done'

for root, dirs, files in os.walk(r'C:\Users\Trevor\Documents\VeriCloud\Cleanedtest'):
        
        for fn in files:
            # Try to parse the file as a MySQL file
            try:
                # Create a Path object for the source file and the CSV output file
                src_path = Path(root).joinpath(fn)
                stem, ext = os.path.splitext(fn)
                csv_dir_path = Path(root).joinpath(stem)
                if csv_dir_path.exists():
                    continue
                csv_dir_path.mkdir(parents=True, exist_ok=True)
                csv_src_path = csv_dir_path.joinpath(f"{stem}.csv")
                sql_src_path = csv_dir_path.joinpath(f"{stem}.sql")
                sql_error_path = csv_dir_path.joinpath(f"{stem}.sql")
                
                # Parse the file with the MySQLParser class
                parser = MySQLParser(str(src_path), config)
                parser.initialize()
                with alive_bar(title=f"Reconstructing Database: {stem}.sql") as bar:
                    parser.construct(bar)
                
                # Look for credentials in the parsed file that match a certain filter
                results = parser.find_credentials(db_filter)
                # Write the results to a CSV file
                write_results_to_file(results, csv_src_path)
                parser.finalize()
                shutil.move(src_path, csv_dir_path)
                
                # Check if the directory for the CSV files contains only the SQL file being parsed
                if len(os.listdir(csv_dir_path)) == 1 and os.listdir(csv_dir_path)[0] == fn:
                    shutil.move(sql_error_path, error_dir)
                
                # Check if folder is empty and attempt to move folder
                folder_path = csv_dir_path
                is_empty = len(os.listdir(folder_path)) == 0

                if is_empty:
                    os.rmdir(folder_path)
                else:
                    try:
                        shutil.move(csv_dir_path, done_dir)
                    except:
                        pass

            
            # If the parsing fails due to too many errors, move the file to an error directory
            except MySQLParserError as e:
                if e.args[0] == MySQLParserError.TOO_MANY_ERRORS:
                    folder_path = csv_dir_path
                    print("Too many errors in file")
                    shutil.move(src_path, os.path.join(error_dir, fn))
                    os.rmdir(folder_path)
                else:
                    raise e