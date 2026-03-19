import os
import sys


def main():
    if len(sys.argv) != 2:
        print("Error: Pass the table name")
        sys.exit(1)

    table_name = sys.argv[1]

    dump = f"export PGPASSWORD=stockey; pg_dump --host=localhost --port=5432 --username=stockey --table=public.{table_name} --data-only --column-inserts stockey > /tmp/{table_name}.sql"
    os.system(dump)
    restore = f"export PGPASSWORD=stockey; psql --host=172.26.39.7 --port=5432 --username=stockey --dbname=stockey --file=/tmp/{table_name}.sql"
    os.system(restore)


if __name__ == "__main__":
    main()
