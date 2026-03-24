from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import requests

from data.dhanlive.auth import get_access_token
from environs import Env


env = Env()
env.read_env()


class DhanAPIError(RuntimeError):
    pass


class DhanHistoricalClient:
    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, access_token: str | None = None, timeout: int = 60):
        self.access_token = access_token or get_access_token()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "access-token": self.access_token,
            }
        )

    def validate_access_token(self) -> dict[str, Any]:
        response = self.session.get(f"{self.BASE_URL}/profile", timeout=self.timeout)
        return self._parse_response(response)

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
        response = self.session.post(
            f"{self.BASE_URL}/charts/historical",
            json=payload,
            timeout=self.timeout,
        )
        return self._parse_response(response)

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
        response = self.session.post(
            f"{self.BASE_URL}/charts/intraday",
            json=payload,
            timeout=self.timeout,
        )
        return self._parse_response(response)

    def _parse_response(self, response: requests.Response) -> dict[str, Any]:
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
        response = self.session.post(
            f"{self.BASE_URL}/orders",
            json=payload,
            timeout=self.timeout,
        )
        return self._parse_response(response)

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
        response = self.session.put(
            f"{self.BASE_URL}/orders/{order_id}",
            json=payload,
            timeout=self.timeout,
        )
        return self._parse_response(response)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        response = self.session.delete(
            f"{self.BASE_URL}/orders/{order_id}",
            timeout=self.timeout,
        )
        return self._parse_response(response)

    def get_orders(self) -> list[dict[str, Any]] | dict[str, Any]:
        response = self.session.get(
            f"{self.BASE_URL}/orders",
            timeout=self.timeout,
        )
        return self._parse_response(response)

    def get_order_by_id(self, order_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.BASE_URL}/orders/{order_id}",
            timeout=self.timeout,
        )
        return self._parse_response(response)

    def get_order_by_correlation_id(self, correlation_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.BASE_URL}/orders/external/{correlation_id}",
            timeout=self.timeout,
        )
        return self._parse_response(response)

    def get_trades(self) -> list[dict[str, Any]] | dict[str, Any]:
        response = self.session.get(
            f"{self.BASE_URL}/trades",
            timeout=self.timeout,
        )
        return self._parse_response(response)

    def get_trades_by_order_id(self, order_id: str) -> list[dict[str, Any]] | dict[str, Any]:
        response = self.session.get(
            f"{self.BASE_URL}/trades/{order_id}",
            timeout=self.timeout,
        )
        return self._parse_response(response)


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
