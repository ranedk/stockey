from __future__ import annotations

import json
from datetime import timezone

import pandas as pd
from environs import Env

from utils.db import upsert_to_db
from utils.http import get_with_retries
from utils.schema_migrations import apply_schema_migration

from .sharpely_utils import get_sharpely_headers

STOCKEY_RUN_STATE: dict[str, object] = {}


def get_latest_from_sharpely(headers):
    # instrumentType=2 is equity (feeds master_sharpely_equity -> company_master's sharpely_id
    # fallback and sector_data.py's NSE->BSE sector-code mapping). Types 0/1 (non-stock entities --
    # ETFs, MFs) used to also be fetched here and written to master_sharpely_funds; retired
    # 2026-08-15 (zero readers anywhere, confirmed live) along with that table, so this no longer
    # requests them at all -- one less unnecessary call to the sharpely/mintbox API per run.
    # sharpely_id: the API stopped returning it (2026-09-24: absent from the response's
    # keys, 0 of 346k stored rows had one) -- kept so the column stays; company_master
    # derives ids from tickers regardless. isin: now returned, and the one identifier
    # that survives an NSE symbol rename, so it is stored.
    stock_keys = [
        "sharpely_id",
        "isin",
        "symbol",
        "bse_ticker",
        "proper_name",
        "lseg_instrument_id",
        "nse_active",
        "nse_segment",
        "bse_segment",
        "sector_code",
    ]

    resp = get_with_retries(
        "https://pyapiv2.mintbox.ai/api/core/getAllFundsV2/instrumentType=2",
        headers=headers,
    ).json()
    data = json.loads(resp)
    response_keys = list(data["keys"])

    _data = []
    for val in json.loads(data["values"]):
        row_map = dict(zip(response_keys, val))
        _data.append({key: row_map.get(key) for key in stock_keys})

    return pd.DataFrame(_data, columns=stock_keys)


def _dedupe_and_fix_unique_key() -> None:
    """The unique key (symbol, bse_ticker) treated NULLs as distinct, so every NSE-only
    company (bse_ticker NULL) was INSERTED again on every run: 340,783 rows for 905
    companies by 2026-09-24, multiplied into security_dimension's join. Keep one row per
    key, and make NULLs compare equal so ON CONFLICT catches them from now on."""
    apply_schema_migration(
        migration_id="20260924_master_sharpely_equity_dedupe_nulls_not_distinct",
        description="master_sharpely_equity: dedupe; UNIQUE NULLS NOT DISTINCT (symbol, bse_ticker).",
        owner="data.sharpelydata.scrip_master",
        metadata={"tables": ["master_sharpely_equity"]},
        statements=[
            # One pass (window over ctid). A self-join on IS NOT DISTINCT FROM cannot use the
            # index and ran >10 min holding the migrations lock on 2026-09-24 -- cancelled.
            "DELETE FROM master_sharpely_equity WHERE ctid IN ("
            " SELECT ctid FROM (SELECT ctid, row_number() OVER ("
            "   PARTITION BY symbol, coalesce(bse_ticker, '') ORDER BY ctid DESC) AS rn"
            "   FROM master_sharpely_equity) ranked WHERE rn > 1)",
            "ALTER TABLE master_sharpely_equity DROP CONSTRAINT IF EXISTS master_sharpely_equity_symbol_bse_ticker_key",
            "ALTER TABLE master_sharpely_equity ADD CONSTRAINT master_sharpely_equity_symbol_bse_ticker_key "
            "UNIQUE NULLS NOT DISTINCT (symbol, bse_ticker)",
        ],
    )


def update_masters() -> dict[str, object]:
    env = Env()
    env.read_env()

    headers = get_sharpely_headers()
    df_equity = get_latest_from_sharpely(headers)
    raw_equity_rows = len(df_equity)
    df_equity = df_equity[
        ~((df_equity["symbol"].isna()) & (df_equity["bse_ticker"].isna()))
    ]

    _dedupe_and_fix_unique_key()
    upsert_to_db(
        df_equity,
        "master_sharpely_equity",
        unique_keys=["symbol", "bse_ticker"],
    )
    load_ts = pd.Timestamp.now(tz=timezone.utc)
    return {
        "source": "sharpely",
        "rows": int(len(df_equity)),
        "rows_read": raw_equity_rows,
        "rows_written": int(len(df_equity)),
        "equity_rows": int(len(df_equity)),
        "raw_equity_rows": raw_equity_rows,
        "load_ts": load_ts.isoformat(),
        "fallback_used": False,
        "state_advanced": True,
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    STOCKEY_RUN_STATE = update_masters()
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
