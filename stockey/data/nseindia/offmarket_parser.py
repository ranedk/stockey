# offmarket parser
import pandas as pd
from environs import Env
import redis

from utils.db import upsert_to_db
from utils import store


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "bhav:parsed"

rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def process_csv(file_name, csv_path):
    """Process a block deals, bulk deals and short selling CSV file."""
    df = pd.read_csv(csv_path)
    if "block_deals" in file_name:
        dtype = "block_deals"
        unique_keys = ["date", "symbol", "client_name", "buysell"]
        df.columns = ["date", "symbol", "security_name", "client_name", "buysell", "quantity", "price", "remarks"]
    elif "bulk_deals" in file_name:
        dtype = "bulk_deals"
        unique_keys = ["date", "symbol", "client_name", "buysell"]
        df.columns = ["date", "symbol", "security_name", "client_name", "buysell", "quantity", "price", "remarks"]
    elif "short_selling" in file_name:
        dtype = "short_selling"
        unique_keys = ["date", "symbol"]
        df.columns = ["date", "symbol", "security_name", "quantity"]
    else:
        raise ValueError("file %s type not supported" % file_name)

    df['date'] = pd.to_datetime(df['date'])
    upsert_to_db(df, f"nseindia_{dtype}", unique_keys=unique_keys)
    rop.sadd(REDIS_SET, file_name)
    

if __name__ == "__main__":
    for f in store.list_files("nsedeals"):
        if rop.sismember(REDIS_SET, f):
            print(f"⏩ Already parsed: {f}")
            continue

        file_path = store.get_as_temp_file(f)
        process_csv(f, file_path)
        rop.sadd(REDIS_SET, f)

    rop.close()