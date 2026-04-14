# bhavcopy download from S3 and parse
import io
import tempfile
import zipfile
import glob
import os
from datetime import datetime, timedelta
from collections.abc import Iterable

import pandas as pd
from environs import Env
import redis

from utils.company_master import attach_company_master_id
from utils.db import sql_to_df, upsert_to_db
from utils.ingestion_state import get_failed_entries, get_processed_keys, mark_failed, mark_processed
from utils import store
from utils.date import pd_to_datetime, remove_invalid_dates
from utils.sync import get_redis_client


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "bhav:parsed"
SOURCE_PREFIX = "bhavcopy"
NSE_BHAVCOPY_PARSE_LOOKBACK_DAYS = max(env.int("NSE_BHAVCOPY_PARSE_LOOKBACK_DAYS", 365), 1)

rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))


def emit(message: str) -> None:
    print(message, flush=True)


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def extract_bhavcopy_date_from_key(key: str) -> pd.Timestamp | None:
    try:
        return pd.to_datetime(os.path.basename(key), format="bhavcopy_%Y-%m-%d.zip")
    except ValueError:
        return None


def should_consider_key(key: str, *, today: datetime | None = None) -> bool:
    today = today or datetime.today()
    file_date = extract_bhavcopy_date_from_key(key)
    if file_date is None:
        return False
    cutoff = pd.Timestamp((today - timedelta(days=NSE_BHAVCOPY_PARSE_LOOKBACK_DAYS)).date())
    return file_date.normalize() >= cutoff


def load_existing_ohlcv_dates(target_dates: Iterable[pd.Timestamp]) -> set[str]:
    normalized = sorted({pd.Timestamp(value).strftime("%Y-%m-%d") for value in target_dates if pd.notna(value)})
    if not normalized:
        return set()

    existing: set[str] = set()
    for batch in chunked(normalized, 500):
        placeholders = ", ".join(["%s"] * len(batch))
        query = f"""
            SELECT DISTINCT date::date AS parsed_date
            FROM nseindia_ohlcv
            WHERE date::date IN ({placeholders})
        """
        try:
            frame = sql_to_df(query, params=tuple(batch))
        except Exception as exc:
            message = str(exc).lower()
            if exc.__class__.__name__ == "UndefinedTable" or "does not exist" in message:
                return set()
            raise
        if frame.empty:
            continue
        existing.update(
            pd.to_datetime(frame["parsed_date"], errors="coerce")
            .dropna()
            .dt.strftime("%Y-%m-%d")
            .tolist()
        )
    return existing


def run_parser() -> None:
    files = [key for key in store.list_files("bhavcopy") if should_consider_key(key)]
    keyed_dates = {key: extract_bhavcopy_date_from_key(key) for key in files}
    processed_keys = get_processed_keys(SOURCE_PREFIX)
    parsed_dates = load_existing_ohlcv_dates(
        value for value in keyed_dates.values() if value is not None
    )
    failed_entries = {row["object_key"]: row for row in get_failed_entries(SOURCE_PREFIX)}

    if failed_entries:
        emit(f"⚠️ Found {len(failed_entries)} previously failed bhavcopy key(s) in DB state")
        for key in sorted(failed_entries)[:10]:
            row = failed_entries[key]
            emit(f"⚠️ Prior failure key={key} at={row.get('processed_at')} error={row.get('error_message')}")

    for key in files:
        if key in processed_keys:
            emit(f"⏩ Already processed in DB state: {key}")
            continue
        parsed_date = keyed_dates.get(key)
        if parsed_date is not None and parsed_date.strftime("%Y-%m-%d") in parsed_dates:
            emit(f"⏩ Already parsed in DB: {key}")
            mark_processed(SOURCE_PREFIX, key)
            processed_keys.add(key)
            continue
        file_path = store.get_as_temp_file(key)
        emit(f"For: {key}")
        try:
            parsed = unzip_and_process(file_path)
        except Exception as exc:
            error_message = f"{exc.__class__.__name__}: {exc}"
            emit(f"❌ Failed to parse bhavcopy key={key}: {error_message}")
            mark_failed(SOURCE_PREFIX, key, error_message)
            continue
        if parsed:
            mark_processed(SOURCE_PREFIX, key)
            processed_keys.add(key)
            rop.sadd(REDIS_SET, key)
        else:
            emit(f"⏭️ Marking bhavcopy key as processed without OHLCV rows: {key}")
            mark_processed(SOURCE_PREFIX, key)
            processed_keys.add(key)

    rop.close()


def with_company_master(df: pd.DataFrame) -> pd.DataFrame:
    return attach_company_master_id(df, ticker_column="symbol", exchange="NSE")


def parse_mcap(path):
    emit("Processing MCAP")
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
    emit("Processing Circuit Hit")
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
    emit("Processing Corporate Actions BC")
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
    emit("Processing BhavCopy")
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
    emit("Processing OHLCV")
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
    emit("Processing REG")
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
    emit("Processing PE")
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
    emit("Processing MTO")
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
    emit("Processing CSQR")
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
    emit("Processing CMVOL")
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
    emit("Processing VAR1")
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
    emit("Processing CAT Turnover")
    try:
        df = pd.read_excel(path, sheet_name="Daily", skiprows=2, header=None)
    except ValueError:
        try:
            df = pd.read_excel(path, skiprows=3, header=None)
        except Exception as exc:
            raise RuntimeError(f"Unable to read CAT Turnover workbook fallback path={path}") from exc
    except Exception as exc:
        raise RuntimeError(f"Unable to read CAT Turnover workbook path={path}") from exc

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
    emit("Processing CATG")
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


def is_empty_zip(path: str) -> bool:
    try:
        return os.path.getsize(path) == 0
    except OSError:
        return False


def unzip_and_process(zip_path):
    emit("Processing %s" % zip_path)
    if is_empty_zip(zip_path):
        emit(f"⏭️ Skipping empty zip: {zip_path}")
        os.remove(zip_path)
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)
        except zipfile.BadZipFile as exc:
            emit(f"❌ Bad bhavcopy zip: {zip_path} - {exc}")
            os.remove(zip_path)
            raise RuntimeError(f"bad_bhavcopy_zip:{os.path.basename(zip_path)}") from exc

        failures: list[str] = []

        def _run(label: str, file_path: str, parser) -> None:
            try:
                parser(file_path)
            except Exception as exc:
                failures.append(f"{label}:{os.path.basename(file_path)}:{exc.__class__.__name__}:{exc}")
                emit(f"❌ Failed {label} file={file_path}: {exc.__class__.__name__}: {exc}")

        catg_files = glob.glob(os.path.join(tmpdir, "**", "C_CATG_*.T*"), recursive=True)
        for file_path in catg_files:
            _run("catg", file_path, parse_catg)

        var1_files = glob.glob(os.path.join(tmpdir, "**", "C_VAR1_*_*.DAT"), recursive=True)
        for file_path in var1_files:
            _run("var1", file_path, parse_var1)

        cat_turnover_files = glob.glob(os.path.join(tmpdir, "**", "cat_turnover_*.xls"), recursive=True)
        for file_path in cat_turnover_files:
            _run("cat_turnover", file_path, parse_cat_turnover)

        cmvolt_files = glob.glob(os.path.join(tmpdir, "**", "CMVOLT_*.CSV"), recursive=True)
        for file_path in cmvolt_files:
            _run("cmvolt", file_path, parse_cmvolt)

        csqr_files = glob.glob(os.path.join(tmpdir, "**", "CSQR_*.CSV"), recursive=True)
        for file_path in csqr_files:
            _run("csqr", file_path, parse_csqr)

        mto_files = glob.glob(os.path.join(tmpdir, "**", "MTO_*.CSV"), recursive=True)
        for file_path in mto_files:
            _run("mto", file_path, parse_mto)

        pe_files = glob.glob(os.path.join(tmpdir, "**", "PE_*.CSV"), recursive=True)
        for file_path in pe_files:
            _run("pe", file_path, parse_pe)

        reg_files = glob.glob(os.path.join(tmpdir, "**", "REG_*.CSV"), recursive=True)
        for file_path in reg_files:
            _run("reg", file_path, parse_reg)

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "cm*.zip"), recursive=True)
        for nested_zip in nested_zips:
            if is_empty_zip(nested_zip):
                emit(f"⏭️ Skipping empty nested zip: {nested_zip}")
                continue
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                try:
                    with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                        nested_ref.extractall(nested_tmpdir)
                except zipfile.BadZipFile as exc:
                    failures.append(f"nested_cm_zip:{os.path.basename(nested_zip)}:{exc.__class__.__name__}:{exc}")
                    emit(f"❌ Bad nested CM zip: {nested_zip} - {exc}")
                    continue

                cm_files = glob.glob(os.path.join(nested_tmpdir, "**", "cm*.csv"), recursive=True)
                for file_path in cm_files:
                    if not os.path.isdir(file_path):
                        _run("ohlcv", file_path, parse_ohlcv)

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "BhavCopy*.zip"), recursive=True)
        for nested_zip in nested_zips:
            if is_empty_zip(nested_zip):
                emit(f"⏭️ Skipping empty nested zip: {nested_zip}")
                continue
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                try:
                    with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                        nested_ref.extractall(nested_tmpdir)
                except zipfile.BadZipFile as exc:
                    failures.append(f"nested_bhavcopy_zip:{os.path.basename(nested_zip)}:{exc.__class__.__name__}:{exc}")
                    emit(f"❌ Bad nested BhavCopy zip: {nested_zip} - {exc}")
                    continue

                bhav_files = glob.glob(os.path.join(nested_tmpdir, "**", "BhavCopy*.csv"), recursive=True)
                for file_path in bhav_files:
                    if not os.path.isdir(file_path):
                        _run("bhavcopy", file_path, parse_bhavcopy)

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "PR*.zip"), recursive=True)
        for nested_zip in nested_zips:
            if is_empty_zip(nested_zip):
                emit(f"⏭️ Skipping empty nested zip: {nested_zip}")
                continue
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                try:
                    with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                        nested_ref.extractall(nested_tmpdir)
                except zipfile.BadZipFile as exc:
                    failures.append(f"nested_pr_zip:{os.path.basename(nested_zip)}:{exc.__class__.__name__}:{exc}")
                    emit(f"❌ Bad nested PR zip: {nested_zip} - {exc}")
                    continue

                bc_files = glob.glob(os.path.join(nested_tmpdir, "**", "Bc*.csv"), recursive=True)
                for file_path in bc_files:
                    _run("corporate_actions_bc", file_path, parse_corporate_actions_bc)

                bh_files = glob.glob(os.path.join(nested_tmpdir, "**", "bh*.csv"), recursive=True)
                for file_path in bh_files:
                    _run("circuit_hit", file_path, parse_circuit_hit)

                mcap_files = glob.glob(os.path.join(nested_tmpdir, "**", "MCAP*.csv"), recursive=True)
                for file_path in mcap_files:
                    _run("mcap", file_path, parse_mcap)

        if failures:
            emit(f"⚠️ Completed bhavcopy zip with {len(failures)} file-level failure(s): {zip_path}")
            raise RuntimeError("; ".join(failures[:20]))

    os.remove(zip_path)
    return True


if __name__ == "__main__":
    run_parser()
