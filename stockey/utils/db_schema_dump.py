#!/usr/bin/env python3
import os, argparse
import psycopg2
from collections import defaultdict
from utils.db import get_connection

EXCLUDE_SCHEMAS = {"pg_catalog", "information_schema"}

SQL_TABLES = """
SELECT n.nspname AS schema, c.relname AS table
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind='r'
  AND n.nspname = ANY(%s)
ORDER BY 1,2;
"""

SQL_COLUMNS = """
SELECT n.nspname, c.relname, a.attname,
       pg_catalog.format_type(a.atttypid, a.atttypmod),
       NOT a.attnotnull AS nullable,
       pg_get_expr(ad.adbin, ad.adrelid)
FROM pg_attribute a
JOIN pg_class c ON a.attrelid=c.oid
JOIN pg_namespace n ON c.relnamespace=n.oid
LEFT JOIN pg_attrdef ad ON a.attrelid=ad.adrelid AND a.attnum=ad.adnum
WHERE a.attnum>0 AND NOT a.attisdropped
  AND n.nspname = ANY(%s) AND c.relkind='r'
ORDER BY 1,2,a.attnum;
"""

SQL_PKS = """
SELECT n.nspname AS schema,
       c.relname  AS table,
       a.attname  AS column
FROM pg_constraint con
JOIN pg_class      c  ON con.conrelid = c.oid
JOIN pg_namespace  n  ON c.relnamespace = n.oid
JOIN LATERAL unnest(con.conkey) AS k(attnum) ON TRUE   -- <— alias
JOIN pg_attribute  a  ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE con.contype = 'p'
  AND n.nspname = ANY(%s);
"""

SQL_INDEXES = """
SELECT n.nspname, t.relname, i.relname, ix.indisunique,
       array_agg(a.attname ORDER BY ord) AS cols
FROM pg_class t
JOIN pg_namespace n ON n.oid=t.relnamespace
JOIN pg_index ix ON ix.indrelid=t.oid
JOIN pg_class i ON i.oid=ix.indexrelid
JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
WHERE t.relkind='r' AND n.nspname = ANY(%s)
GROUP BY 1,2,3,4
ORDER BY 1,2,3;
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", default="", help="Comma-separated schema list (default: all except system)")
    args = ap.parse_args()

    with get_connection() as conn, conn.cursor() as cur:
        # figure schemas
        if args.schemas:
            schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]
        else:
            cur.execute("SELECT nspname FROM pg_namespace;")
            schemas = [s for (s,) in cur.fetchall() if s not in EXCLUDE_SCHEMAS]

        # collect
        cur.execute(SQL_TABLES, (schemas,))
        tables = [(s, t) for s, t in cur.fetchall()]

        cur.execute(SQL_COLUMNS, (schemas,))
        colmap = defaultdict(list)
        for s, t, col, typ, nullable, default in cur.fetchall():
            colmap[(s, t)].append({"name": col, "type": typ,
                                   "nullable": nullable, "default": default})

        cur.execute(SQL_PKS, (schemas,))
        pkmap = defaultdict(set)
        for s, t, col in cur.fetchall():
            pkmap[(s, t)].add(col)

        cur.execute(SQL_INDEXES, (schemas,))
        idxmap = defaultdict(list)
        col_to_idx = defaultdict(lambda: defaultdict(list))
        for s, t, idx, uniq, cols in cur.fetchall():
            idxmap[(s, t)].append({"name": idx, "unique": uniq, "cols": cols})
            for c in cols:
                col_to_idx[(s, t)][c].append(idx)

    # print
    for schema, table in tables:
        if schema.startswith("_"):
            continue

        print(f"\n{schema}.{table}")
        for c in colmap[(schema, table)]:
            flags = []
            if c["name"] in pkmap[(schema, table)]: flags.append("PK")
            if not c["nullable"]: flags.append("NOT NULL")
            if c["default"] is not None: flags.append(f"DEFAULT={c['default']}")
            # if col_to_idx[(schema, table)].get(c["name"]):
            #     flags.append("IDX:" + ",".join(col_to_idx[(schema, table)][c["name"]]))
            print(f"  - {c['name']}: {c['type']} {' '.join(flags)}")

        if idxmap[(schema, table)]:
            print("  # Indexes")
            for idx in idxmap[(schema, table)]:
                u = "UNIQUE " if idx["unique"] else ""
                print(f"    {idx['name']}: {u}({', '.join(idx['cols'])})")

if __name__ == "__main__":
    main()
