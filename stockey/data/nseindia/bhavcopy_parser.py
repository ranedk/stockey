from datetime import datetime
import pandas as pd
from utils import upsert_to_db


def parse_catg(path):
    with open(path) as f:
        header = f.readline().strip().split(',')
        for_month = datetime.strptime(f"01,{header[1]},{header[2]}", "%d,%b,%Y").date()
    df = pd.read_csv(path, skiprows=1)
    df = df.reset_index(drop=True)
    df.columns = ['record_type','symbol','series','isin','category','impact_cost']
    df = df.drop(columns='record_type')
    df['category'] = pd.to_numeric(df['category'], errors='coerce').astype('Int64')
    df['impact_cost'] = pd.to_numeric(df['impact_cost'], errors='coerce')
    df['for_month'] = pd.to_datetime(for_month)
    upsert_to_db(df, "nseindia_catg", unique_keys=["for_month", "isin"])
    return df

def parse_var1(path):
    with open(path) as f:
        parts = f.readline().strip().split(',')
        for_date = datetime.strptime(parts[1], "%d%m%Y").date()
        if len(parts) == 4:
            entry_number = 1
        else:
            entry_number = int(parts[3])

    df = pd.read_csv(path, skiprows=1)
    df.columns = [
        'record_type','symbol','series','isin',
        'security_var','index_var','var_margin',
        'extreme_loss_rate','adhoc_margin','applicable_margin'
    ]
    df = df.drop(columns='record_type')
    for c in ['security_var','index_var','var_margin','extreme_loss_rate','adhoc_margin','applicable_margin']:
        df[c] = pd.to_numeric(df[c], errors='coerce')

    df['for_date'] = pd.to_datetime(for_date)
    df['entry_number'] = entry_number
    upsert_to_db(df, "nseindia_var1", unique_keys=["for_date", "entry_number", "isin"])
    return df


def parse_cat_turnover(path):
    df = pd.read_excel(path, sheet_name="Daily", skiprows=2, header=None)
    df.columns = ['trade_date','client_category','buy_rs_cr','sell_rs_cr']
    df['trade_date'] = pd.to_datetime(df['trade_date'], format='%d %b %y', errors='coerce')
    df['buy_rs_cr'] = pd.to_numeric(df['buy_rs_cr'], errors='coerce')
    df['sell_rs_cr'] = pd.to_numeric(df['sell_rs_cr'], errors='coerce')
    df.dropna(subset=["trade_date", "client_category", "buy_rs_cr", "sell_rs_cr"], inplace=True)
    upsert_to_db(df, "nseindia_cat_turnover", unique_keys=["trade_date", "client_category"])
    return df
