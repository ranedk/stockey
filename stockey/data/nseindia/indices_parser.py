# bhavcopy download from S3 and parse
import tempfile
import zipfile
import glob
import os
from datetime import datetime, timedelta

import pandas as pd
from environs import Env
import redis

from utils.db import upsert_to_db
from utils.ingestion_state import get_failed_entries, get_processed_keys, mark_failed, mark_processed
from utils import store
from utils.sync import get_redis_client


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "indices:parsed"
SOURCE_PREFIX = "indices"
NSE_INDICES_PARSE_LOOKBACK_DAYS = max(env.int("NSE_INDICES_PARSE_LOOKBACK_DAYS", 365), 1)

rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))


def emit(message: str) -> None:
    print(message, flush=True)


def extract_indices_date_from_key(key: str) -> str | None:
    filename = os.path.basename(key)
    try:
        return datetime.strptime(filename, "indices_%Y-%m-%d.zip").strftime("%Y-%m-%d")
    except ValueError:
        return None


def should_consider_key(key: str, *, today: datetime | None = None) -> bool:
    today = today or datetime.today()
    file_date = extract_indices_date_from_key(key)
    if file_date is None:
        return False
    cutoff = (today - timedelta(days=NSE_INDICES_PARSE_LOOKBACK_DAYS)).date()
    return datetime.strptime(file_date, "%Y-%m-%d").date() >= cutoff


def is_empty_file(path: str) -> bool:
    try:
        return os.path.getsize(path) == 0
    except OSError:
        return False


def parse_indices_close(path):
    df = pd.read_csv(path)
    df.columns = [
        "index_name",
        "date",
        "open",
        "high",
        "low",
        "close",
        "points_change",
        "percent_change",
        "volume",
        "turnover_cr",
        "pe",
        "pb",
        "div_yield",
    ]

    df = df.reset_index(drop=True)
    df["index_name"] = df["index_name"].astype("string").str.strip()
    try:
        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
    except ValueError:
        df["date"] = pd.to_datetime(df["date"], format="%d/%m/%Y")
    for col in [
        "open",
        "high",
        "low",
        "close",
        "points_change",
        "percent_change",
        "volume",
        "turnover_cr",
        "pe",
        "pb",
        "div_yield",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "index_name"])
    df = df.drop_duplicates(subset=["date", "index_name"], keep="last")

    upsert_to_db(
        df,
        "nseindia_indices",
        unique_keys=["date", "index_name"],
        timescaledb_column="date"
    )
    return df


def unzip_and_process(zip_path):
    emit("Processing %s" % zip_path)
    if is_empty_file(zip_path):
        emit(f"⏭️ Skipping empty indices zip: {zip_path}")
        os.remove(zip_path)
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)
        except zipfile.BadZipFile as exc:
            emit(f"⏭️ Skipping bad indices zip: {zip_path} - {exc}")
            os.remove(zip_path)
            return False

        indices_close_files = glob.glob(os.path.join(tmpdir, "ind_close_*.csv"))
        for file_path in indices_close_files:
            parse_indices_close(file_path)

    os.remove(zip_path)
    return True


if __name__ == "__main__":
    parsed_files = get_processed_keys(SOURCE_PREFIX)
    failed_entries = {row["object_key"]: row for row in get_failed_entries(SOURCE_PREFIX)}
    if failed_entries:
        emit(f"⚠️ Found {len(failed_entries)} previously failed indices key(s) in DB state")
        for key in sorted(failed_entries)[:10]:
            row = failed_entries[key]
            emit(f"⚠️ Prior failure key={key} at={row.get('processed_at')} error={row.get('error_message')}")
    for f in store.list_files("indices"):
        if not should_consider_key(f):
            continue
        if f in parsed_files:
            emit(f"⏩ Already parsed in DB state: {f}")
            continue
        file_date = extract_indices_date_from_key(f)
        file_path = store.get_as_temp_file(f)
        emit(
            f"For indices key={f} date={file_date or 'unknown'} temp_file={file_path}"
        )
        try:
            parsed = unzip_and_process(file_path)
        except Exception as exc:
            error_message = f"{exc.__class__.__name__}: {exc}"
            emit(f"❌ Failed to parse indices key={f}: {error_message}")
            mark_failed(SOURCE_PREFIX, f, error_message)
            continue
        if parsed:
            mark_processed(SOURCE_PREFIX, f)
            rop.sadd(REDIS_SET, f)
            parsed_files.add(f)
        else:
            error_message = "Parser completed without extracting index rows"
            emit(f"❌ Failed to fully parse indices key={f}: {error_message}")
            mark_failed(SOURCE_PREFIX, f, error_message)
    rop.close()
