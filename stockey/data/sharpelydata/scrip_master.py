import json
from utils.http import get_with_retries, get_dynamic_headers
from utils.duck import upsert_to_duckdb_auto
from .sharpely_utils import get_sharpely_headers
import pandas as pd
from environs import Env


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
        all_keys = data["keys"]

        for val in json.loads(data["values"]):
            row = [v for k, v in zip(data["keys"], val) if k in keys_of_interest[i]]
            _data.append(row)

        df = pd.DataFrame(_data, columns=keys_of_interest[i])
        dfs.append(df)

    return dfs


def update_masters():
    env = Env()
    env.read_env()

    headers = get_sharpely_headers()
    dfs = get_latest_from_sharpely(headers)
    df_funds = pd.concat([dfs[0], dfs[1]])
    df_funds = df_funds.dropna(subset=["amfi_code"])

    df_equity = dfs[2]
    df_equity[~((df_equity["symbol"].isna()) & (df_equity["bse_ticker"].isna()))]


    upsert_to_duckdb_auto(
        df_funds,
        env("DUCKDB"),
        env("SCHEMA"),
        "funds_master",
        unique_keys=["amfi_code"],
    )
    upsert_to_duckdb_auto(
        df_equity,
        env("DUCKDB"),
        env("SCHEMA"),
        "equity_master",
        unique_keys=["symbol", "bse_ticker"],
    )


"""
resp = requests.get(f"https://pyapiv2.mintbox.ai/api/core/getFinancialStatementsV2/ticker={ticker}", headers=headers).json()
fin = json.loads(resp['statements'])

from IPython import embed
embed()


Use this to get lseg_instrument_id to

https://pyapiv2.mintbox.ai/api/core/getShareHoldingsDataV1/symbol=SHAKTIPUMP
https://pyapiv2.mintbox.ai/api/core/getStockProfile/symbol=SHAKTIPUMP
https://pyapiv2.mintbox.ai/api/core/stock_insights_detailed/ticker=SHAKTIPUMP
https://pyapiv2.mintbox.ai/api/core/getCorporateActionsV2/lseg_instrument_id=8590071662

"""

if __name__ == "__main__":
    update_masters()
