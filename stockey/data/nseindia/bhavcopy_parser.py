# bhavcopy download from S3 and parse
import io
import tempfile
import zipfile
import glob
import os
from datetime import datetime

import pandas as pd
from environs import Env
import redis

from utils.company_master import attach_company_master_id
from utils.db import upsert_to_db
from utils import store
from utils.date import pd_to_datetime, remove_invalid_dates


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "bhav:parsed"

rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def with_company_master(df: pd.DataFrame) -> pd.DataFrame:
    return attach_company_master_id(df, ticker_column="symbol", exchange="NSE")


def parse_mcap(path):
    print("Processing MCAP")
    data = open(path).read()
    lines = [line.strip().rstrip(".") for line in data.strip().split("\n")]
    cleaned_data = "\n".join(lines[:-3])
    df = pd.read_csv(io.StringIO(cleaned_data), skiprows=1)
    df = df.reset_index(drop=True)
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    df.columns = [
        "trade_date",
        "symbol",
        "series",
        "security_name",
        "category",
        "last_trade_date",
        "face_value_rs",
        "issue_size",
        "close_price_paid_up_value_rs",
        "market_cap_rs",
    ]
    df["date"] = pd.to_datetime(df["trade_date"], format="%d %b %Y")
    df = df.drop(columns=["trade_date", "security_name"])
    df["issue_size"] = pd.to_numeric(df["issue_size"], errors="coerce").astype("Int64")
    for c in ["face_value_rs", "close_price_paid_up_value_rs", "market_cap_rs"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    unique_keys = ["date", "symbol", "series"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_mcap", unique_keys=unique_keys)
    return df


def parse_circuit_hit(path):
    print("Processing Circuit Hit")
    parts = os.path.basename(path)
    try:
        for_date = pd.to_datetime(parts, format="bh%d%m%y.csv")
    except ValueError: # Format issue
        for_date = pd.to_datetime(parts, format="bh%d%m%Y.csv")

    df = pd.read_csv(path, usecols=[0, 1, 3], encoding='utf-8', encoding_errors='ignore')
    df.columns = ["symbol", "series", "circuit_hit"]
    df["date"] = for_date
    unique_keys = ["date", "symbol", "series", "circuit_hit"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(
        df,
        "nseindia_circuit_hit",
        unique_keys=unique_keys,
    )
    return df


def parse_corporate_actions_bc(path):
    print("Processing Corporate Actions BC")
    df = pd.read_csv(path)
    df.columns = [
        "series",
        "symbol",
        "security_name",
        "record_date",
        "bc_start_date",
        "bc_end_date",
        "date",
        "nd_start_date",
        "nd_end_date",
        "subject",
    ]
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)

    for col in ["record_date", "bc_start_date", "bc_end_date", "date", "nd_start_date", "nd_end_date"]:
        s = (
            df[col]
            .astype("string")
            .str.strip()
            .replace({"": pd.NA, "-": pd.NA, "None": pd.NA, "null": pd.NA})
        )
        df[col] = pd.to_datetime(s, format="%d/%m/%Y", errors="coerce")

    df = df.dropna(subset=["date", "symbol", "series", "subject"])
    unique_keys = ["date", "symbol", "series", "subject"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_corporate_actions_bc_raw", unique_keys=unique_keys, timescaledb_column="date")
    return df

def parse_bhavcopy(csv_path):
    print("Processing BhavCopy")
    df = pd.read_csv(csv_path)
    cmap = {
        'TradDt': 'date',
        'ISIN': 'isin',
        'TckrSymb': 'symbol',
        'SctySrs': 'series',
        'OpnPric': 'open',
        'HghPric': 'high',
        'LwPric': 'low',
        'ClsPric': 'close',
        'LastPric': 'last',
        'PrvsClsgPric': 'previous_close',
        'TtlTradgVol': 'volume',
        'TtlTrfVal': 'total_value',
        'TtlNbOfTxsExctd': 'number_of_trades',
    }
    df = df[list(cmap.keys())]
    df.columns = list(cmap.values())
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    for c in [
        "open",
        "high",
        "low",
        "close",
        "last",
        "previous_close",
        "volume",
        "total_value",
        "number_of_trades",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = pd_to_datetime(df, "date", formats=["%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"])
    unique_keys = ["date", "symbol", "series"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_ohlcv", unique_keys=unique_keys)
    return df


def parse_ohlcv(csv_path):
    print("Processing OHLCV")
    df = pd.read_csv(csv_path, skiprows=1)
    df = df.iloc[:, :13]
    df = df.reset_index(drop=True)
    df.columns = [
        "symbol",
        "series",
        "open",
        "high",
        "low",
        "close",
        "last",
        "previous_close",
        "volume",
        "total_value",
        "date",
        "number_of_trades",
        "isin",
    ]
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    for c in [
        "open",
        "high",
        "low",
        "close",
        "last",
        "previous_close",
        "volume",
        "total_value",
        "number_of_trades",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = pd_to_datetime(df, "date", formats=["%d-%b-%Y", "%d-%b-%y"])
    unique_keys = ["date", "symbol", "series"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_ohlcv", unique_keys=unique_keys)
    return df


def parse_reg(path):
    print("Processing REG")
    parts = path.split("_")[-1].split(".")[0]
    for_date = pd.to_datetime(parts, format="IND%d%m%y")
    df = pd.read_csv(path, skiprows=1)
    df = df.reset_index(drop=True)
    df.columns = [
        "scrip_code",
        "symbol",
        "nse_exclusive",
        "status",
        "series",
        "gsm",
        "long_term_asm",
        "unsolicited_sms",
        "irp",
        "short_term_asm",
        "default",
        "ica",
        "filler4",
        "filler5",
        "pledge",
        "add_on_pb",
        "total_pledge",
        "social_media_platforms",
        "esm",
        "loss_making",
        "encumbered_share_gt_50pct",
        "under_bz_sz_series",
        "annual_listing_fee_default",
        "filler12",
        "fo_contracts_moved_out",
        "filler13",
        "filler14",
        "filler15",
        "filler16",
    ]
    df.drop(
        columns=[
            "scrip_code",
            "filler4",
            "filler5",
            "filler12",
            "filler13",
            "filler14",
            "filler15",
            "filler16",
        ],
        inplace=True,
    )
    for c in [
        "gsm",
        "long_term_asm",
        "unsolicited_sms",
        "irp",
        "short_term_asm",
        "default",
        "ica",
        "pledge",
        "add_on_pb",
        "total_pledge",
        "social_media_platforms",
        "esm",
        "loss_making",
        "encumbered_share_gt_50pct",
        "under_bz_sz_series",
        "annual_listing_fee_default",
        "fo_contracts_moved_out",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = for_date
    unique_keys = ["date", "symbol"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_reg", unique_keys=unique_keys)
    return df


def parse_pe(path):
    print("Processing PE")
    parts = path.split("_")[-1].split(".")[0]
    for_date = pd.to_datetime(parts, format="%d%m%y")
    df = pd.read_csv(path, skiprows=1)
    df = df.reset_index(drop=True)
    df.columns = ["symbol", "pe", "adjusted_pe"]
    for c in ["pe", "adjusted_pe"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = for_date
    unique_keys = ["date", "symbol"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_pe", unique_keys=unique_keys)
    return df


def parse_mto(path):
    print("Processing MTO")
    parts = path.split("_")[-1].split(".")[0]
    for_date = pd.to_datetime(parts, format="%d%m%Y")
    df = pd.read_csv(path, skiprows=4)
    df = df.reset_index(drop=True)
    df.columns = [
        "record_type",
        "sr_no",
        "symbol",
        "series",
        "volume",
        "deliverable_volume",
        "deliverable_percent",
    ]
    df["date"] = for_date
    for c in ["volume", "deliverable_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    df["deliverable_percent"] = pd.to_numeric(
        df["deliverable_percent"], errors="coerce"
    )
    unique_keys = ["date", "symbol"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_mto", unique_keys=unique_keys)
    return df


def parse_csqr(path):
    print("Processing CSQR")
    parts = path.split("_")[-1].split(".")[0]
    for_date = pd.to_datetime(parts, format="%d%m%Y")
    df = pd.read_csv(path)
    df = df.reset_index(drop=True)
    df.columns = [
        "symbol",
        "series",
        "market_type",
        "settlement_number",
        "official_close",
    ]
    df["date"] = for_date
    df["settlement_number"] = pd.to_numeric(
        df["settlement_number"], errors="coerce"
    ).astype("Int64")
    df["official_close"] = pd.to_numeric(df["official_close"], errors="coerce")
    unique_keys = ["date", "symbol", "settlement_number"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(
        df, "nseindia_csqr", unique_keys=unique_keys
    )
    return df


def parse_cmvolt(path):
    print("Processing CMVOL")
    df = pd.read_csv(path, skiprows=1)
    df = df.iloc[:, :8]
    df = df.reset_index(drop=True)
    df.columns = [
        "date",
        "symbol",
        "close",
        "previous_close",
        "log_return",
        "previous_day_daily_volatility",
        "current_day_daily_volatility",
        "annualized_volatility",
    ]
    df["date"] = pd.to_datetime(df["date"])
    for c in [
        "close",
        "previous_close",
        "log_return",
        "previous_day_daily_volatility",
        "current_day_daily_volatility",
        "annualized_volatility",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    unique_keys = ["date", "symbol"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_cmvolt", unique_keys=unique_keys)
    return df


def parse_var1(path):
    print("Processing VAR1")
    with open(path) as f:
        parts = f.readline().strip().split(",")
        for_date = datetime.strptime(parts[1], "%d%m%Y").date()
        if len(parts) == 4:
            entry_number = 1
        else:
            entry_number = int(parts[3])

    df = pd.read_csv(path, skiprows=1)
    df.columns = [
        "record_type",
        "symbol",
        "series",
        "isin",
        "security_var",
        "index_var",
        "var_margin",
        "extreme_loss_rate",
        "adhoc_margin",
        "applicable_margin",
    ]
    df = df.drop(columns="record_type")
    for c in [
        "security_var",
        "index_var",
        "var_margin",
        "extreme_loss_rate",
        "adhoc_margin",
        "applicable_margin",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["for_date"] = pd.to_datetime(for_date)
    df["entry_number"] = entry_number
    unique_keys = ["for_date", "entry_number", "series", "symbol", "isin"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_var1", unique_keys=unique_keys)
    return df


def parse_cat_turnover(path):
    print("Processing CAT Turnover")
    try:
        df = pd.read_excel(path, sheet_name="Daily", skiprows=2, header=None)
    except ValueError:
        df = pd.read_excel(path, skiprows=3, header=None)
    except:
        return

    df = df.iloc[:, :4]
    df.columns = ["trade_date", "client_category", "buy_rs_cr", "sell_rs_cr"]
    df.dropna(
        subset=["trade_date", "client_category", "buy_rs_cr", "sell_rs_cr"],
        inplace=True,
    )
    df = remove_invalid_dates(df, "trade_date")
    df = pd_to_datetime(df, "trade_date", ["%d %b %y", "%d-%b-%y"], errors="coerce")
    df["buy_rs_cr"] = pd.to_numeric(df["buy_rs_cr"], errors="coerce")
    df["sell_rs_cr"] = pd.to_numeric(df["sell_rs_cr"], errors="coerce")
    upsert_to_db(
        df, "nseindia_cat_turnover", unique_keys=["trade_date", "client_category"]
    )
    return df


def parse_catg(path):
    print("Processing CATG")
    with open(path) as f:
        header = f.readline().strip().split(",")
        for_month = datetime.strptime(f"01,{header[1]},{header[2]}", "%d,%b,%Y").date()
    df = pd.read_csv(path, skiprows=1)
    df = df.reset_index(drop=True)
    df.columns = ["record_type", "symbol", "series", "isin", "category", "impact_cost"]
    df = df.drop(columns="record_type")
    df["category"] = pd.to_numeric(df["category"], errors="coerce").astype("Int64")
    df["impact_cost"] = pd.to_numeric(df["impact_cost"], errors="coerce")
    df["for_month"] = pd.to_datetime(for_month)

    # ISIN is not unique, there can be multiple Symbols with the same ISIN because the same underlying
    # can be traded in different series (e.g. Nifty 50 and Nifty 50 Future)
    # Also, the same symbol and isin can be a part of more than one series e.g. SHAKTIPUMP is traded in
    # series BE and EQ
    # When company changes its name, symbol also changes but ISIN remains the same
    df = with_company_master(df)
    upsert_to_db(
        df, "nseindia_catg", unique_keys=["for_month", "series", "symbol", "isin"]
    )
    return df


def unzip_and_process(zip_path):
    print("Processing %s" % zip_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(tmpdir)

        catg_files = glob.glob(os.path.join(tmpdir, "**", "C_CATG_*.T*"), recursive=True)
        for file_path in catg_files:
            parse_catg(file_path)

        var1_files = glob.glob(os.path.join(tmpdir, "**", "C_VAR1_*_*.DAT"), recursive=True)
        for file_path in var1_files:
            parse_var1(file_path)

        cat_turnover_files = glob.glob(os.path.join(tmpdir, "**", "cat_turnover_*.xls"), recursive=True)
        for file_path in cat_turnover_files:
            parse_cat_turnover(file_path)

        cmvolt_files = glob.glob(os.path.join(tmpdir, "**", "CMVOLT_*.CSV"), recursive=True)
        for file_path in cmvolt_files:
            parse_cmvolt(file_path)

        csqr_files = glob.glob(os.path.join(tmpdir, "**", "CSQR_*.CSV"), recursive=True)
        for file_path in csqr_files:
            parse_csqr(file_path)

        mto_files = glob.glob(os.path.join(tmpdir, "**", "MTO_*.CSV"), recursive=True)
        for file_path in mto_files:
            parse_mto(file_path)

        pe_files = glob.glob(os.path.join(tmpdir, "**", "PE_*.CSV"), recursive=True)
        for file_path in pe_files:
            parse_pe(file_path)

        reg_files = glob.glob(os.path.join(tmpdir, "**", "REG_*.CSV"), recursive=True)
        for file_path in reg_files:
            parse_reg(file_path)

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "cm*.zip"), recursive=True)
        for nested_zip in nested_zips:
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                    nested_ref.extractall(nested_tmpdir)

                cm_files = glob.glob(os.path.join(nested_tmpdir, "**", "cm*.csv"), recursive=True)
                for file_path in cm_files:
                    if not os.path.isdir(file_path):
                        parse_ohlcv(file_path)

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "BhavCopy*.zip"), recursive=True)
        for nested_zip in nested_zips:
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                    nested_ref.extractall(nested_tmpdir)

                bhav_files = glob.glob(os.path.join(nested_tmpdir, "**", "BhavCopy*.csv"), recursive=True)
                for file_path in bhav_files:
                    if not os.path.isdir(file_path):
                        parse_bhavcopy(file_path)

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "PR*.zip"), recursive=True)
        for nested_zip in nested_zips:
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                    nested_ref.extractall(nested_tmpdir)

                bc_files = glob.glob(os.path.join(nested_tmpdir, "**", "Bc*.csv"), recursive=True)
                for file_path in bc_files:
                    parse_corporate_actions_bc(file_path)

                bh_files = glob.glob(os.path.join(nested_tmpdir, "**", "bh*.csv"), recursive=True)
                for file_path in bh_files:
                    parse_circuit_hit(file_path)

                mcap_files = glob.glob(os.path.join(nested_tmpdir, "**", "MCAP*.csv"), recursive=True)
                for file_path in mcap_files:
                    parse_mcap(file_path)

    os.remove(zip_path)


if __name__ == "__main__":
    parsed_sites = rop.smembers(REDIS_SET)
    for f in store.list_files("bhavcopy"):
        if f in parsed_sites:
            print(f"⏩ Already parsed: {f}")
            continue
        file_path = store.get_as_temp_file(f)
        print(f"For: {f}")
        unzip_and_process(file_path)
        rop.sadd(REDIS_SET, f)
    rop.close()
