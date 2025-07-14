import time
from datetime import date, timedelta
import redis
import requests

from utils.http import get_with_retries, get_dynamic_headers


REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_SET = "fbilgec:downloaded"
HEADERS = get_dynamic_headers()
rdb = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def get_cookies():
    response = requests.get('https://www.fbil.org.in/', headers=HEADERS)
    return response.cookies.get_dict()


def download_gsec(formatted_date: str, cookies):
    if rdb.sismember(REDIS_SET, formatted_date):
        return
    params = {
        #'date': '2025-06-05', # format date
        'date': formatted_date
    }
    response = get_with_retries('https://www.fbil.org.in/wasdm/gsec/downloadPublished', params=params, cookies=cookies, headers=HEADERS)
    with open(f"fbil_gsec_{formatted_date}.xls", "wb") as f:
        f.write(response.content)
    rdb.sadd(REDIS_SET, formatted_date)


def main():
    cookies = get_cookies()
    today = date.today()
    n = 1
    while n <= 3650:
        fdate = today - timedelta(days=n)
        download_gsec(fdate.strftime("%Y-%m-%d"), cookies=cookies)
        time.sleep(2)
        n += 1


if __name__ == "__main__":
    main()
