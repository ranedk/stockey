# bhavcopy download from S3 and parse
import tempfile
import zipfile
import glob
import os
from datetime import datetime

import pandas as pd
from environs import Env
import redis

from utils.db import upsert_to_db
from utils import store
from utils.sync import get_redis_client, get_redis_set_members


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "indices:parsed"

rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))


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
    print("Processing %s" % zip_path)
    if is_empty_file(zip_path):
        print(f"⏭️ Skipping empty indices zip: {zip_path}")
        os.remove(zip_path)
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)
        except zipfile.BadZipFile as exc:
            print(f"⏭️ Skipping bad indices zip: {zip_path} - {exc}")
            os.remove(zip_path)
            return False

        indices_close_files = glob.glob(os.path.join(tmpdir, "ind_close_*.csv"))
        for file_path in indices_close_files:
            parse_indices_close(file_path)

    os.remove(zip_path)
    return True


if __name__ == "__main__":
    parsed_files = get_redis_set_members(rop, REDIS_SET)
    for f in store.list_files("indices"):
        if f in parsed_files:
            print(f"⏩ Already parsed: {f}")
            continue
        file_path = store.get_as_temp_file(f)
        parsed = unzip_and_process(file_path)
        if parsed:
            rop.sadd(REDIS_SET, f)
            parsed_files.add(f)
    rop.close()
