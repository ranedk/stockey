# bhavcopy download from S3 and parse
import io
import shutil
import tempfile
import zipfile
import glob
import os
from datetime import datetime, timedelta

import pandas as pd
from environs import Env
import redis

from utils.date import reverse_daterange
from utils.db import upsert_to_db
from utils import store


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "bhav:parsed"

rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def parse_mcap(path):
    data = open(path).read()
    lines = [line.strip().rstrip(".") for line in data.strip().split("\n")]
    cleaned_data = "\n".join(lines[:-3])
    df = pd.read_csv(io.StringIO(cleaned_data), skiprows=1)
    df = df.reset_index(drop=True)
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    df.columns =[
        'trade_date',
        'symbol',
        'series',
        'security_name',
        'category',
        'last_trade_date',
        'face_value_rs',
        'issue_size',
        'close_price_paid_up_value_rs',
        'market_cap_rs'
    ]
    df['date'] = pd.to_datetime(df['trade_date'], format="%d %b %Y")
    df = df.drop(columns=["trade_date", "security_name"])
    df['issue_size'] = pd.to_numeric(df['issue_size'], errors="coerce").astype("Int64")
    for c in [
        'face_value_rs',
        'close_price_paid_up_value_rs',
        'market_cap_rs'
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    upsert_to_db(df, "nseindia_mcap", unique_keys=["date", "symbol", "series"])
    return df


def parse_circuit_hit(path):
    parts = os.path.basename(path)
    for_date = pd.to_datetime(parts, format="bh%d%m%y.csv")
    df = pd.read_csv(path, usecols=[0, 1, 3])
    df.columns = ["symbol", "series", "circuit_hit"]
    df["date"] = for_date
    upsert_to_db(df, "nseindia_circuit_hit", unique_keys=["date", "symbol", "series", "circuit_hit"])
    return df


def parse_sme_bhavdata(path):
    parts = os.path.basename(path)
    for_date = pd.to_datetime(parts, format="sme%d%m%y.csv")
    df = pd.read_csv(path, skiprows=1)
    df = df.reset_index(drop=True)
    df.columns = [
        "market",
        "series",
        "symbol",
        "security_name",
        "previous_close",
        "open",
        "high",
        "low",
        "close",
        "net_traded_value",
        "net_traded_qty",
        "corp_indicator",
        "high_52_week",
        "low_52_week",
    ]
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    for c in [
        "previous_close",
        "open",
        "high",
        "low",
        "close",
        "net_traded_value",
        "net_traded_qty",
        "high_52_week",
        "low_52_week",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = for_date
    upsert_to_db(df, "nseindia_sme_bhavdata", unique_keys=["date", "symbol", "series"])
    return df


def parse_sec_bhavdata(path):
    df = pd.read_csv(path, skiprows=1)
    df = df.reset_index(drop=True)
    df.columns = [
        "symbol",
        "series",
        "date",
        "previous_close",
        "open",
        "high",
        "low",
        "last_traded_price",
        "close",
        "average",
        "total_traded_quantity",
        "turnover_lacs",
        "number_of_trades",
        "delivery_quantity",
        "deliverable_percent",
    ]
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    for c in [
        "previous_close",
        "open",
        "high",
        "low",
        "last_traded_price",
        "close",
        "average",
        "total_traded_quantity",
        "turnover_lacs",
        "number_of_trades",
        "delivery_quantity",
        "deliverable_percent",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], format="%d-%b-%Y")
    upsert_to_db(df, "nseindia_sec_bhavdata", unique_keys=["date", "symbol", "series"])
    return df


def parse_reg(path):
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
    upsert_to_db(df, "nseindia_reg", unique_keys=["date", "symbol"])
    return df


def parse_pe(path):
    parts = path.split("_")[-1].split(".")[0]
    for_date = pd.to_datetime(parts, format="%d%m%y")
    df = pd.read_csv(path, skiprows=1)
    df = df.reset_index(drop=True)
    df.columns = ["symbol", "pe", "adjusted_pe"]
    for c in ["pe", "adjusted_pe"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = for_date
    upsert_to_db(df, "nseindia_pe", unique_keys=["date", "symbol"])
    return df


def parse_mto(path):
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
    upsert_to_db(df, "nseindia_mto", unique_keys=["date", "symbol"])
    return df


def parse_csqr(path):
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
    upsert_to_db(
        df, "nseindia_csqr", unique_keys=["date", "symbol", "settlement_number"]
    )
    return df


def parse_cmvolt(path):
    df = pd.read_csv(path, skiprows=1)
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
    upsert_to_db(df, "nseindia_cmvolt", unique_keys=["date", "symbol"])
    return df


def parse_var1(path):
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
    upsert_to_db(df, "nseindia_var1", unique_keys=["for_date", "entry_number", "series", "symbol", "isin"])
    return df


def parse_cat_turnover(path):
    df = pd.read_excel(path, sheet_name="Daily", skiprows=2, header=None)
    df.columns = ["trade_date", "client_category", "buy_rs_cr", "sell_rs_cr"]
    df["trade_date"] = pd.to_datetime(
        df["trade_date"], format="%d %b %y", errors="coerce"
    )
    df["buy_rs_cr"] = pd.to_numeric(df["buy_rs_cr"], errors="coerce")
    df["sell_rs_cr"] = pd.to_numeric(df["sell_rs_cr"], errors="coerce")
    df.dropna(
        subset=["trade_date", "client_category", "buy_rs_cr", "sell_rs_cr"],
        inplace=True,
    )
    upsert_to_db(
        df, "nseindia_cat_turnover", unique_keys=["trade_date", "client_category"]
    )
    return df


def parse_catg(path):
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
    upsert_to_db(df, "nseindia_catg", unique_keys=["for_month", "series", "symbol", "isin"])
    return df

def unzip_and_process(zip_path):
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(tmpdir)

        catg_files = glob.glob(os.path.join(tmpdir, "C_CATG_*.T*"))
        for file_path in catg_files:
            parse_catg(file_path)

        var1_files = glob.glob(os.path.join(tmpdir, "C_VAR1_*_*.DAT"))
        for file_path in var1_files:
            parse_var1(file_path)

        cat_turnover_files = glob.glob(os.path.join(tmpdir, "cat_turnover_*.xls"))
        for file_path in cat_turnover_files:
            parse_cat_turnover(file_path)

        cmvolt_files = glob.glob(os.path.join(tmpdir, "CMVOLT_*.CSV"))
        for file_path in cmvolt_files:
            parse_cmvolt(file_path)

        csqr_files = glob.glob(os.path.join(tmpdir, "CSQR_*.CSV"))
        for file_path in csqr_files:
            parse_csqr(file_path)

        mto_files = glob.glob(os.path.join(tmpdir, "MTO_*.CSV"))
        for file_path in mto_files:
            parse_mto(file_path)

        pe_files = glob.glob(os.path.join(tmpdir, "PE_*.CSV"))
        for file_path in pe_files:
            parse_pe(file_path)

        reg_files = glob.glob(os.path.join(tmpdir, "REG_*.CSV"))
        for file_path in reg_files:
            parse_reg(file_path)

        sec_bhavdata_files = glob.glob(os.path.join(tmpdir, "sec_bhavdata_*.CSV"))
        for file_path in sec_bhavdata_files:
            parse_sec_bhavdata(file_path)

        sme_bhavdata_files = glob.glob(os.path.join(tmpdir, "sme*.CSV"))
        for file_path in sme_bhavdata_files:
            parse_sme_bhavdata(file_path)

        nested_zips = glob.glob(os.path.join(tmpdir, "PR*.zip"))
        for nested_zip in nested_zips:
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                    nested_ref.extractall(nested_tmpdir)

                bh_files = glob.glob(os.path.join(nested_tmpdir, "bh*.csv"))
                for file_path in bh_files:
                    parse_circuit_hit(file_path)

                mcap_files = glob.glob(os.path.join(nested_tmpdir, "MCAP*.csv"))
                for file_path in mcap_files:
                    parse_mcap(file_path)

    shutil.rmtree(zip_path)



if __name__ == "__main__":
    for f in store.list_files("bhavcopy"):
        if rop.sismember(REDIS_SET, f):
            print(f"⏩ Already parsed: {f}")
            continue
        file_path = store.get_as_temp_file(f)
        unzip_and_process(file_path)
        rop.sadd(REDIS_SET, f)
    rop.close()
