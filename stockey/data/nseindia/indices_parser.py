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

from utils.db import upsert_to_db
from utils import store


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "indices:parsed"

rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


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
        "div_yield"
    ]

    df = df.reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
    for col in ["open", "high", "low", "close", "points_change", "percent_change", "volume", "turnover_cr", "pe", "pb", "div_yield"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    
    upsert_to_db(df, "nseindia_indices", unique_keys=["date", "index_name"], timescaledb_column="date")
    return df

def unzip_and_process(zip_path):
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(tmpdir)

        indices_close_files = glob.glob(os.path.join(tmpdir, "ind_close_*.csv"))
        for file_path in indices_close_files:
            parse_indices_close(file_path)


if __name__ == "__main__":
    for f in store.list_files("indices"):
        if rop.sismember(REDIS_SET, f):
            print(f"⏩ Already parsed: {f}")
            continue
        file_path = store.get_as_temp_file(f)
        unzip_and_process(file_path)
        rop.sadd(REDIS_SET, f)
    rop.close()
