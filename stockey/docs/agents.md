# LLM / Agent Integration Notes

## Recommended split

If you expose this repo to agent workflows, keep it as separate tool-owning agents instead of one general agent with full access:

- `downloader`: runs crawler and downloader modules only
- `storage-ops`: runs `scripts/sql_query_runner.py`, `scripts/redis_query_runner.py`, `scripts/s3_query_runner.py`
- `ml-research`: read-only SQL plus notebook or training code
- `investment-analyst`: read-only SQL plus fundamental tables
- `technical-analyst`: read-only SQL plus price, volume, and market structure tables
- `macro-geopolitical`: read-only SQL plus macro tables and external news tools
- `sentiment`: news ingestion and sentiment pipelines

## Tool contract

Prefer wrapping these existing scripts as tools first. They already have stable JSON output:

- `python scripts/sql_query_runner.py --read-only ...`
- `python scripts/redis_query_runner.py ...`
- `python scripts/s3_query_runner.py ...`
- `python scripts/agent_tool_runner.py list`
- `python scripts/agent_tool_runner.py run sql_query -- --read-only "select * from macro_usa limit 5"`

Each tool wrapper should:

- pass arguments explicitly, not through shell interpolation
- enforce `--read-only` for analyst-style agents
- capture stdout as JSON
- treat non-zero exit codes as hard failures

## MCP direction

If you convert this into an MCP server later, the easiest first step is to expose:

1. `sql_query`
2. `redis_scan`
3. `redis_get`
4. `s3_list`
5. `s3_head`
6. curated ingestion commands for approved loaders

Do not expose arbitrary shell execution to the model. Keep scrape/download tools curated per source.

## Current curated registry

The repository now includes:

- [`docs/tool_registry.json`](/home/rane/code/stockey/docs/tool_registry.json): approved tool definitions
- [`scripts/agent_tool_runner.py`](/home/rane/code/stockey/scripts/agent_tool_runner.py): a thin registry-based runner

Usage:

```sh
python scripts/agent_tool_runner.py list
python scripts/agent_tool_runner.py list --category storage
python scripts/agent_tool_runner.py run sql_query -- --read-only "select * from macro_usa order by date desc limit 5"
python scripts/agent_tool_runner.py run redis_scan -- --pattern 'nse:*'
python scripts/agent_tool_runner.py run load_us_macro --allow-writes
```

The runner blocks non-read-only tools unless `--allow-writes` is passed. That is the right default for analyst-style agents.

The runner resolves `python` and `python3` commands to the invoking interpreter, so launching it with `python` keeps downstream tools inside the project virtualenv.

## Data modeling advice

Before adding more agents, finish these pieces:

- keep raw ingestion tables separate from feature tables
- keep one authoritative trading calendar
- standardize table names and unique keys
- document per-table date semantics
- prefer numeric SQL types at ingest time instead of repairing them later

## Advisory system roadmap

The current advisory roadmap is tracked in [`todo.md`](/home/rane/code/stockey/todo.md). Use that file for active priorities and bottlenecks before exposing new advisory tools to agents.
