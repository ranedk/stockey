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
    # Non-Stock entities - ETFs, MFs etc.
    fund_keys = [
        "plan_id",
        "isin_code",
        "amfi_code",
        "sharpely_id",
        "regular_plan_id",
        "nse_symbol",
        "bse_symbol",
        "bse_scheme_code",
        "basic_name",
        "is_index_fund",
        "is_etf_fund",
        "is_fof",
        "is_dividend",
        "variant",
        "variant_fund_id",
        "amc_full_name",
        "category_id",
        "category_name",
        "sharpely_bm_id",
        "plan_name",
        "type_id",
        "is_direct_plan",
        "objective_text",
    ]

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

    keys_of_interest = {0: fund_keys, 1: fund_keys, 2: stock_keys}

    dfs = []
    for i in [0, 1, 2]:
        _data = []
        resp = get_with_retries(
            f"https://pyapiv2.mintbox.ai/api/core/getAllFundsV2/instrumentType={i}",
            headers=headers,
        ).json()
        data = json.loads(resp)
        response_keys = list(data["keys"])

        for val in json.loads(data["values"]):
            row_map = dict(zip(response_keys, val))
            _data.append({key: row_map.get(key) for key in keys_of_interest[i]})

        df = pd.DataFrame(_data, columns=keys_of_interest[i])
        dfs.append(df)

    return dfs


def update_masters() -> dict[str, object]:
    env = Env()
    env.read_env()

    headers = get_sharpely_headers()
    dfs = get_latest_from_sharpely(headers)
    df_funds = pd.concat([dfs[0], dfs[1]])
    df_funds = df_funds.dropna(subset=["amfi_code"])

    df_equity = dfs[2]
    df_equity = df_equity[
        ~((df_equity["symbol"].isna()) & (df_equity["bse_ticker"].isna()))
    ]

    upsert_to_db(
        df_funds,
        "master_sharpely_funds",
        unique_keys=["amfi_code"],
    )
    upsert_to_db(
        df_equity,
        "master_sharpely_equity",
        unique_keys=["symbol", "bse_ticker"],
    )
    load_ts = pd.Timestamp.now(tz=timezone.utc)
    return {
        "source": "sharpely",
        "rows": int(len(df_funds) + len(df_equity)),
        "rows_read": int(sum(len(df) for df in dfs)),
        "rows_written": int(len(df_funds) + len(df_equity)),
        "fund_rows": int(len(df_funds)),
        "equity_rows": int(len(df_equity)),
        "raw_fund_rows": int(len(dfs[0]) + len(dfs[1])),
        "raw_equity_rows": int(len(dfs[2])),
        "instrument_type_count": int(len(dfs)),
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
