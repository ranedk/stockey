# Utility Commands

## Dump one table locally

```sh
export PGPASSWORD=stockey
pg_dump --host=localhost --port=5432 --username=stockey --table=public.<table_name> --data-only stockey > <table_name>.sql
```

## Restore one table to a server

```sh
export PGPASSWORD=stockey
psql --host=<server_ip> --port=5432 --username=stockey --dbname=stockey --file=<table_name>.sql
```

## Refresh schema summary

```sh
python -m utils.db_schema_dump --schemas public
```

## Review security identity issues

```sh
python -m data.nseindia.security_history
/home/rane/code/stockey/.xstockey/bin/python scripts/sql_query_runner.py --read-only "select * from dim_security_review_events where needs_review = true order by confidence desc, last_seen desc limit 50"
```

Manual overrides go into `dim_security_overrides`. Use them for mergers, demergers, scheme changes, and any rename the heuristics flag incorrectly.
