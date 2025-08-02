# Dump from localhost and tranfer to server

`export PGPASSWORD=stockey; pg_dump --host=localhost --port=5432 --username=stockey --table=public.<table_name> --data-only stockey > <table_name>.sql`

`export PGPASSWORD=stockey; psql --host=172.26.39.7 --port=5432 --username=stockey --dbname=stockey --file=<table_name>.sql`
