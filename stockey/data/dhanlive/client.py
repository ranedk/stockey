from __future__ import annotations

from datetime import datetime
import sys
from typing import Any

import pandas as pd
import requests

from data.dhanlive.auth import force_refresh_access_token, get_access_token
from environs import Env


env = Env()
env.read_env()


class DhanAPIError(RuntimeError):
    pass


class DhanHistoricalClient:
    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, access_token: str | None = None, timeout: int = 60, auth_attempts: int | None = None):
        self.access_token = access_token or get_access_token()
        self.timeout = timeout
        self.auth_attempts = max(int(auth_attempts or env.int("DHAN_API_AUTH_ATTEMPTS", default=3)), 1)
        self.session = requests.Session()
        self._set_access_token(self.access_token)

    def _set_access_token(self, access_token: str) -> None:
        self.access_token = access_token
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "access-token": self.access_token,
            }
        )

    def validate_access_token(self) -> dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/profile")

    def fetch_daily(
        self,
        *,
        security_id: int | str,
        exchange_segment: str,
        instrument: str,
        from_date: datetime,
        to_date: datetime,
        oi: bool = False,
        expiry_code: int = 0,
    ) -> dict[str, Any]:
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "expiryCode": expiry_code,
            "oi": oi,
            "fromDate": from_date.strftime("%Y-%m-%d"),
            "toDate": to_date.strftime("%Y-%m-%d"),
        }
        return self._request("POST", f"{self.BASE_URL}/charts/historical", json=payload)

    def fetch_intraday(
        self,
        *,
        security_id: int | str,
        exchange_segment: str,
        instrument: str,
        interval_minutes: int,
        from_datetime: datetime,
        to_datetime: datetime,
        oi: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": str(interval_minutes),
            "oi": oi,
            "fromDate": from_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": to_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self._request("POST", f"{self.BASE_URL}/charts/intraday", json=payload)

    def _request(self, method: str, url: str, **kwargs) -> dict[str, Any] | list[dict[str, Any]]:
        last_response: requests.Response | None = None
        for attempt in range(1, self.auth_attempts + 1):
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            last_response = response
            if not self._is_auth_failure(response):
                return self._parse_response(response)
            if attempt >= self.auth_attempts:
                break
            print(
                f"[data.dhanlive.client] Dhan auth failed; refreshing token attempt={attempt + 1}/{self.auth_attempts}",
                file=sys.stderr,
                flush=True,
            )
            try:
                self._set_access_token(force_refresh_access_token())
            except Exception as exc:
                if attempt >= self.auth_attempts - 1:
                    raise DhanAPIError(f"Dhan auth refresh failed after {self.auth_attempts} attempts: {exc}") from exc
                print(
                    f"[data.dhanlive.client] Dhan auth refresh failed; retrying login error={exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        return self._parse_response(last_response)

    @staticmethod
    def _is_auth_failure(response: requests.Response) -> bool:
        if response.status_code == 401:
            return True
        text = response.text.lower()
        return response.status_code in {400, 403} and "access token" in text and ("invalid" in text or "expired" in text)

    def _parse_response(self, response: requests.Response | None) -> dict[str, Any] | list[dict[str, Any]]:
        if response is None:
            raise DhanAPIError("Dhan API request failed before receiving a response")
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw_text": response.text}

        if response.ok:
            return payload

        error_message = None
        if isinstance(payload, dict):
            error_message = payload.get("errorMessage") or payload.get("message")
        raise DhanAPIError(
            f"Dhan API request failed with status {response.status_code}: "
            f"{error_message or response.text.strip()}"
        )


class DhanTradingClient(DhanHistoricalClient):
    def __init__(
        self,
        access_token: str | None = None,
        client_id: str | None = None,
        timeout: int = 60,
    ):
        super().__init__(access_token=access_token, timeout=timeout)
        self.client_id = client_id or env("DHAN_CLIENT_ID")

    def place_order(
        self,
        *,
        correlation_id: str,
        transaction_type: str,
        exchange_segment: str,
        product_type: str,
        order_type: str,
        validity: str,
        security_id: int | str,
        quantity: int,
        price: float | None = None,
        trigger_price: float | None = None,
        disclosed_quantity: int | None = None,
        after_market_order: bool = False,
        amo_time: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dhanClientId": self.client_id,
            "correlationId": correlation_id,
            "transactionType": transaction_type,
            "exchangeSegment": exchange_segment,
            "productType": product_type,
            "orderType": order_type,
            "validity": validity,
            "securityId": str(security_id),
            "quantity": int(quantity),
            "disclosedQuantity": int(disclosed_quantity or 0),
            "price": float(price or 0),
            "triggerPrice": float(trigger_price or 0),
            "afterMarketOrder": bool(after_market_order),
            "amoTime": amo_time or "",
        }
        return self._request("POST", f"{self.BASE_URL}/orders", json=payload)

    def modify_order(
        self,
        *,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        disclosed_quantity: int | None = None,
        validity: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"dhanClientId": self.client_id}
        if quantity is not None:
            payload["quantity"] = int(quantity)
        if price is not None:
            payload["price"] = float(price)
        if trigger_price is not None:
            payload["triggerPrice"] = float(trigger_price)
        if disclosed_quantity is not None:
            payload["disclosedQuantity"] = int(disclosed_quantity)
        if validity is not None:
            payload["validity"] = validity
        return self._request("PUT", f"{self.BASE_URL}/orders/{order_id}", json=payload)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"{self.BASE_URL}/orders/{order_id}")

    def get_orders(self) -> list[dict[str, Any]] | dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/orders")

    def get_order_by_id(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/orders/{order_id}")

    def get_order_by_correlation_id(self, correlation_id: str) -> dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/orders/external/{correlation_id}")

    def get_trades(self) -> list[dict[str, Any]] | dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/trades")

    def get_trades_by_order_id(self, order_id: str) -> list[dict[str, Any]] | dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/trades/{order_id}")

    def get_fund_limits(self) -> list[dict[str, Any]] | dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/fundlimit")

    def get_holdings(self) -> list[dict[str, Any]] | dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/holdings")

    def get_positions(self) -> list[dict[str, Any]] | dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/positions")


def candles_to_df(payload: dict[str, Any]) -> pd.DataFrame:
    if not payload:
        return pd.DataFrame()

    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if "open_interest" in payload:
        columns.append("open_interest")

    lengths = {column: len(payload.get(column, [])) for column in columns if column in payload}
    if not lengths:
        return pd.DataFrame()
    expected = max(lengths.values())
    for column, size in lengths.items():
        if size != expected:
            raise ValueError(f"Unexpected Dhan candle payload: {column} has {size} rows, expected {expected}")

    df = pd.DataFrame({column: payload.get(column, []) for column in columns if column in payload})
    if df.empty:
        return df

    numeric_columns = ["open", "high", "low", "close", "volume", "open_interest"]
    for column in numeric_columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    df["source_timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True, errors="coerce")
    return df
