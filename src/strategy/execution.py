"""
Gemini private REST execution client for prediction markets.

Authenticates with HMAC-SHA384 using GEMINI_API_KEY / GEMINI_API_SECRET and
exposes the verbs the live market maker needs:

    get_positions(), get_active_orders(), get_order_history(),
    place_order(), cancel_order(), get_order_status(), get_book(),
    get_active_events(), get_spot()

All methods normalise responses to plain Python dicts, raise
ExecutionError on a non-2xx response, and never silently swallow
exceptions — the runner is responsible for catching them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


PROD_BASE = "https://api.gemini.com"
SANDBOX_BASE = "https://api.sandbox.gemini.com"


class ExecutionError(RuntimeError):
    """Raised when a Gemini REST call fails or returns malformed data."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        endpoint: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.endpoint = endpoint


@dataclass
class OrderResult:
    order_id: str
    client_order_id: str
    instrument: str
    side: str
    outcome: str
    requested_quantity: int
    requested_price: float
    filled_quantity: int
    avg_execution_price: float
    fee: float
    status: str
    is_live: bool
    is_cancelled: bool
    created_at_ms: int | None
    raw: dict = field(default_factory=dict)


class GeminiExecutionClient:
    """Thin private + public REST wrapper for Gemini prediction markets."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        base_url: str = PROD_BASE,
        timeout: float = 10.0,
        dry_run: bool = False,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self.api_secret = (
            api_secret if api_secret is not None else os.environ.get("GEMINI_API_SECRET", "")
        )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run

        self._nonce_lock = threading.Lock()
        self._last_nonce_ms = 0

    # ── auth ────────────────────────────────────────────────────────────────

    def _next_nonce(self) -> str:
        with self._nonce_lock:
            now_ms = int(time.time() * 1000)
            if now_ms <= self._last_nonce_ms:
                now_ms = self._last_nonce_ms + 1
            self._last_nonce_ms = now_ms
            return str(now_ms)

    def _sign(self, endpoint: str, payload: dict) -> dict:
        if not self.api_key or not self.api_secret:
            raise ExecutionError(
                "GEMINI_API_KEY / GEMINI_API_SECRET not set",
                endpoint=endpoint,
            )

        body = dict(payload)
        body["request"] = endpoint
        body["nonce"] = self._next_nonce()
        encoded = json.dumps(body).encode()
        b64_payload = base64.b64encode(encoded).decode()
        signature = hmac.new(
            self.api_secret.encode(),
            b64_payload.encode(),
            hashlib.sha384,
        ).hexdigest()

        return {
            "Content-Type": "text/plain",
            "Content-Length": "0",
            "Cache-Control": "no-cache",
            "X-GEMINI-APIKEY": self.api_key,
            "X-GEMINI-PAYLOAD": b64_payload,
            "X-GEMINI-SIGNATURE": signature,
        }

    # ── HTTP plumbing ──────────────────────────────────────────────────────

    def _private_post(self, endpoint: str, payload: dict | None = None) -> Any:
        body = payload or {}
        headers = self._sign(endpoint, body)
        req = urllib.request.Request(
            f"{self.base_url}{endpoint}", data=b"", headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
                return self._parse_json(raw, endpoint=endpoint)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise ExecutionError(
                f"HTTP {exc.code} on {endpoint}",
                status=exc.code,
                body=text,
                endpoint=endpoint,
            ) from exc
        except urllib.error.URLError as exc:
            raise ExecutionError(
                f"network error on {endpoint}: {exc.reason}",
                endpoint=endpoint,
            ) from exc

    def _public_get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return self._parse_json(response.read(), endpoint=path)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise ExecutionError(
                f"HTTP {exc.code} on {path}",
                status=exc.code,
                body=text,
                endpoint=path,
            ) from exc
        except urllib.error.URLError as exc:
            raise ExecutionError(
                f"network error on {path}: {exc.reason}",
                endpoint=path,
            ) from exc

    @staticmethod
    def _parse_json(raw: bytes, *, endpoint: str) -> Any:
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ExecutionError(
                f"invalid JSON on {endpoint}",
                body=raw[:512].decode("utf-8", errors="replace"),
                endpoint=endpoint,
            ) from exc

    # ── public market data ────────────────────────────────────────────────

    def get_active_events(self) -> list[dict]:
        data = self._public_get(
            "/v1/prediction-markets/events?limit=100&search=BTC&status=active"
        )
        if isinstance(data, dict):
            return list(data.get("data", []))
        return list(data) if isinstance(data, list) else []

    def get_book(self, instrument: str, depth: int = 5) -> dict:
        return self._public_get(
            f"/v1/book/{instrument}?limit_bids={depth}&limit_asks={depth}"
        )

    def get_spot(self) -> float | None:
        try:
            data = self._public_get("/v1/pubticker/BTCUSD")
        except ExecutionError:
            return None
        try:
            return float(data["last"])
        except (KeyError, TypeError, ValueError):
            return None

    # ── private state ─────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        data = self._private_post("/v1/prediction-markets/positions")
        return self._extract_list(data, "positions")

    def get_active_orders(self, limit: int = 100) -> list[dict]:
        data = self._private_post(
            "/v1/prediction-markets/orders/active", {"limit": limit}
        )
        return self._extract_list(data, "orders")

    def get_order_history(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        payload: dict[str, Any] = {"limit": limit}
        if status is not None:
            payload["status"] = status
        data = self._private_post("/v1/prediction-markets/orders/history", payload)
        return self._extract_list(data, "orders")

    def get_order_status(self, order_id: str) -> dict:
        data = self._private_post(
            "/v1/prediction-markets/orders/status", {"orderId": order_id}
        )
        if not isinstance(data, dict):
            raise ExecutionError(
                "order status response was not a dict",
                endpoint="/v1/prediction-markets/orders/status",
            )
        return data

    @staticmethod
    def _extract_list(data: Any, key: str) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get(key)
            if isinstance(inner, list):
                return inner
            inner = data.get("data")
            if isinstance(inner, list):
                return inner
        return []

    # ── order entry ───────────────────────────────────────────────────────

    def place_order(
        self,
        *,
        instrument: str,
        side: str,
        outcome: str,
        quantity: int,
        price: float,
        client_order_id: str,
        time_in_force: str = "immediate-or-cancel",
    ) -> OrderResult:
        if side not in ("buy", "sell"):
            raise ValueError(f"invalid side {side!r}")
        if outcome not in ("yes", "no"):
            raise ValueError(f"invalid outcome {outcome!r}")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if not (0.0 < price < 1.0):
            raise ValueError(f"price {price} outside (0,1)")

        payload = {
            "symbol": instrument,
            "side": side,
            "outcome": outcome,
            "quantity": str(quantity),
            "price": f"{price:.4f}",
            "type": "exchange limit",
            "options": [time_in_force],
            "client_order_id": client_order_id,
        }

        if self.dry_run:
            return OrderResult(
                order_id="dry-run",
                client_order_id=client_order_id,
                instrument=instrument,
                side=side,
                outcome=outcome,
                requested_quantity=quantity,
                requested_price=price,
                filled_quantity=0,
                avg_execution_price=0.0,
                fee=0.0,
                status="dry-run",
                is_live=False,
                is_cancelled=True,
                created_at_ms=int(time.time() * 1000),
                raw=payload,
            )

        data = self._private_post("/v1/prediction-markets/orders/new", payload)
        if not isinstance(data, dict):
            raise ExecutionError(
                "order placement response was not a dict",
                endpoint="/v1/prediction-markets/orders/new",
            )
        return self.normalise_order(
            data,
            requested_quantity=quantity,
            requested_price=price,
            client_order_id=client_order_id,
            instrument=instrument,
            side=side,
            outcome=outcome,
        )

    def cancel_order(self, order_id: str) -> dict:
        return self._private_post(
            "/v1/prediction-markets/orders/cancel", {"orderId": order_id}
        )

    # ── normalisation ─────────────────────────────────────────────────────

    @staticmethod
    def normalise_order(
        raw: dict,
        *,
        requested_quantity: int | None = None,
        requested_price: float | None = None,
        client_order_id: str | None = None,
        instrument: str | None = None,
        side: str | None = None,
        outcome: str | None = None,
    ) -> OrderResult:
        def first(*keys, default=None):
            for key in keys:
                if key in raw and raw[key] is not None:
                    return raw[key]
            return default

        def to_int(value, default=0):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

        def to_float(value, default=0.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        is_live = bool(first("is_live", "isLive", default=False))
        is_cancelled = bool(first("is_cancelled", "isCancelled", default=False))
        status = str(first("status", default="unknown"))

        return OrderResult(
            order_id=str(first("order_id", "orderId", "id", default="")),
            client_order_id=str(
                first("client_order_id", "clientOrderId", default=client_order_id or "")
            ),
            instrument=str(first("symbol", "instrumentSymbol", default=instrument or "")),
            side=str(first("side", default=side or "")),
            outcome=str(first("outcome", default=outcome or "")),
            requested_quantity=to_int(
                first("original_amount", "originalAmount", default=requested_quantity or 0)
            ),
            requested_price=to_float(
                first("price", default=requested_price if requested_price is not None else 0.0)
            ),
            filled_quantity=to_int(
                first("executed_amount", "executedAmount", "filledQuantity", default=0)
            ),
            avg_execution_price=to_float(
                first("avg_execution_price", "avgExecutionPrice", default=0.0)
            ),
            fee=to_float(first("fee", default=0.0)),
            status=status,
            is_live=is_live,
            is_cancelled=is_cancelled,
            created_at_ms=to_int(first("timestampms", "timestampMs", default=0)) or None,
            raw=raw,
        )
