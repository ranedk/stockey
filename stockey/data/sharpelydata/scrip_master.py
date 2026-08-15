from __future__ import annotations

import json
from datetime import timezone

import pandas as pd
from environs import Env

from utils.db import upsert_to_db
from utils.http import get_with_retries

from .sharpely_utils import get_sharpely_headers

STOCKEY_RUN_STATE: dict[str, object] = {}


def get_latest_from_sharpely(headers):
    # instrumentType=2 is equity (feeds master_sharpely_equity -> company_master's sharpely_id
    # fallback and sector_data.py's NSE->BSE sector-code mapping). Types 0/1 (non-stock entities --
    # ETFs, MFs) used to also be fetched here and written to master_sharpely_funds; retired
    # 2026-08-15 (zero readers anywhere, confirmed live) along with that table, so this no longer
    # requests them at all -- one less unnecessary call to the sharpely/mintbox API per run.
    stock_keys = [
        "sharpely_id",
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


def update_masters() -> dict[str, object]:
    env = Env()
    env.read_env()

    headers = get_sharpely_headers()
    df_equity = get_latest_from_sharpely(headers)
    raw_equity_rows = len(df_equity)
    df_equity = df_equity[
        ~((df_equity["symbol"].isna()) & (df_equity["bse_ticker"].isna()))
    ]

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
