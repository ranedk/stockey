import json
from utils.http import get_with_retries, get_dynamic_headers
from utils.duck import upsert_to_duckdb_auto
from . import sharpely_utils as su
import pandas as pd
from environs import Env


HEADERS = su.get_sharpely_headers()


def get_financial_statement(ticker):
    resp = get_with_retries(f"https://pyapiv2.mintbox.ai/api/core/getFinancialStatementsV2/ticker={ticker}", headers=HEADERS).json()
    fin = json.loads(resp['statements'])
    from IPython import embed
    embed()


if __name__ == "__main__":
    get_financial_statement("SHAKTIPUMP")

"""

Use this to get lseg_instrument_id to

https://pyapiv2.mintbox.ai/api/core/getShareHoldingsDataV1/symbol=SHAKTIPUMP
https://pyapiv2.mintbox.ai/api/core/getStockProfile/symbol=SHAKTIPUMP
https://pyapiv2.mintbox.ai/api/core/stock_insights_detailed/ticker=SHAKTIPUMP
https://pyapiv2.mintbox.ai/api/core/getCorporateActionsV2/lseg_instrument_id=8590071662

"""

if __name__ == "__main__":
    update_masters()
