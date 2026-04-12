"""
Gemini execution client for prediction markets and Fast API market data.

When configured with a Fast API websocket URL, live market data and order entry
use Gemini's websocket RPC/stream model. Snapshot-style account reads such as
positions and order history remain on the documented prediction-markets REST
endpoints.
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
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

try:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import ClientConnection, connect
except ImportError:  # pragma: no cover - exercised only when the dependency is absent
    ClientConnection = Any  # type: ignore[assignment]
    ConnectionClosed = RuntimeError  # type: ignore[assignment]
    connect = None


PROD_BASE = "https://api.gemini.com"
SANDBOX_BASE = "https://api.sandbox.gemini.com"


class ExecutionError(RuntimeError):
    """Raised when a Gemini HTTP or websocket request fails."""

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


@dataclass
class FastAPIBookTicker:
    symbol: str
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    update_id: int | None
    event_time_ns: int | None


_UNSET = object()


class GeminiFastAPIClient:
    """Fast API websocket client for public market data and private PM trading."""

    def __init__(
        self,
        ws_url: str,
        *,
        timeout: float = 10.0,
        api_key: str = "",
        api_secret: str = "",
    ):
        self.ws_url = ws_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self.api_secret = api_secret

        self._lock = threading.Lock()
        self._conn: ClientConnection | None = None
        self._subscriptions: set[str] = set()
        self._next_request_id = 0
        self._ticker_cache: dict[str, FastAPIBookTicker] = {}
        self._order_events_by_id: dict[str, dict[str, Any]] = {}
        self._order_events_by_client_id: dict[str, dict[str, Any]] = {}
        self._last_nonce_s = 0

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.close()
            finally:
                self._conn = None
                self._subscriptions.clear()
                self._ticker_cache.clear()
                self._order_events_by_id.clear()
                self._order_events_by_client_id.clear()

    def get_book_ticker(self, symbol: str) -> FastAPIBookTicker | None:
        stream = f"{symbol.lower()}@bookTicker"
        with self._lock:
            self._ensure_connected()
            if stream not in self._subscriptions:
                request_id = self._next_id()
                self._send_json(
                    {
                        "id": request_id,
                        "method": "subscribe",
                        "params": [stream],
                    }
                )
                self._await_response(request_id)
                self._subscriptions.add(stream)

            cached = self._ticker_cache.get(symbol.lower())
            if cached is not None:
                return cached

            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                remaining = max(deadline - time.monotonic(), 0.05)
                message = self._recv_json(timeout=remaining)
                self._process_message(message)
                ticker = self._ticker_cache.get(symbol.lower())
                if ticker is None:
                    continue
                return ticker
        return None

    def call(self, method: str, params: Any) -> Any:
        with self._lock:
            self._ensure_connected()
            request_id = self._next_id()
            self._send_json(
                {
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            return self._await_response(request_id)

    def subscribe(self, streams: list[str]) -> None:
        with self._lock:
            missing = [stream for stream in streams if stream not in self._subscriptions]
            if not missing:
                return
            self._ensure_connected()
            request_id = self._next_id()
            self._send_json(
                {
                    "id": request_id,
                    "method": "subscribe",
                    "params": missing,
                }
            )
            self._await_response(request_id)
            self._subscriptions.update(missing)

    def get_order_event(
        self,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
        timeout: float = 0.0,
    ) -> dict[str, Any] | None:
        with self._lock:
            cached = self._find_order_event(order_id=order_id, client_order_id=client_order_id)
            if cached is not None or timeout <= 0:
                return cached

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = max(deadline - time.monotonic(), 0.05)
                message = self._recv_json(timeout=remaining)
                self._process_message(message)
                cached = self._find_order_event(
                    order_id=order_id,
                    client_order_id=client_order_id,
                )
                if cached is not None:
                    return cached
        return None

    def _ensure_connected(self) -> None:
        if self._conn is not None:
            return
        if connect is None:
            raise ExecutionError(
                "Gemini Fast API support requires the 'websockets' package"
            )
        try:
            headers = self._auth_headers()
            self._conn = connect(
                self.ws_url,
                additional_headers=headers,
                open_timeout=self.timeout,
                close_timeout=self.timeout,
                ping_interval=20,
                ping_timeout=self.timeout,
                proxy=None,
            )
        except Exception as exc:  # noqa: BLE001
            raise ExecutionError(
                f"failed to connect to Gemini Fast API at {self.ws_url}: {exc}"
            ) from exc

    def _send_json(self, payload: dict[str, Any]) -> None:
        if self._conn is None:
            raise ExecutionError("Fast API websocket is not connected")
        try:
            self._conn.send(json.dumps(payload))
        except ConnectionClosed:
            self._conn = None
            raise ExecutionError("Gemini Fast API websocket closed while sending") from None
        except Exception as exc:  # noqa: BLE001
            raise ExecutionError(f"Fast API send failed: {exc}") from exc

    def _recv_json(self, *, timeout: float) -> Any:
        if self._conn is None:
            raise ExecutionError("Fast API websocket is not connected")
        try:
            raw = self._conn.recv(timeout=timeout)
        except TimeoutError:
            return None
        except ConnectionClosed:
            self._conn = None
            raise ExecutionError("Gemini Fast API websocket closed while receiving") from None
        except Exception as exc:  # noqa: BLE001
            raise ExecutionError(f"Fast API receive failed: {exc}") from exc

        if raw in (None, ""):
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _next_id(self) -> str:
        self._next_request_id += 1
        return str(self._next_request_id)

    def _next_nonce_seconds(self) -> str:
        now_s = int(time.time())
        if now_s <= self._last_nonce_s:
            now_s = self._last_nonce_s + 1
        self._last_nonce_s = now_s
        return str(now_s)

    def _auth_headers(self) -> dict[str, str] | None:
        if not self.api_key and not self.api_secret:
            return None
        if not self.api_key or not self.api_secret:
            raise ExecutionError("Gemini Fast API auth requires both api key and secret")
        nonce = self._next_nonce_seconds()
        payload = base64.b64encode(nonce.encode()).decode()
        signature = hmac.new(
            self.api_secret.encode(),
            payload.encode(),
            hashlib.sha384,
        ).hexdigest()
        return {
            "X-GEMINI-APIKEY": self.api_key,
            "X-GEMINI-NONCE": nonce,
            "X-GEMINI-PAYLOAD": payload,
            "X-GEMINI-SIGNATURE": signature,
        }

    def _await_response(self, request_id: str) -> Any:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 0.05)
            message = self._recv_json(timeout=remaining)
            response = self._maybe_response_for(message, request_id)
            if response is not _UNSET:
                return response
            self._process_message(message)
        raise ExecutionError(
            f"timed out waiting for Fast API response to request {request_id}"
        )

    def _maybe_response_for(self, message: Any, request_id: str) -> Any:
        if not isinstance(message, dict):
            return _UNSET
        if str(message.get("id", "")) != request_id:
            return _UNSET

        status = message.get("status")
        if status != 200:
            error = message.get("error") or {}
            code = error.get("code")
            detail = error.get("msg") or message
            raise ExecutionError(
                f"Fast API request failed ({code}): {detail}",
                status=int(status) if isinstance(status, int) else None,
                body=json.dumps(message),
            )
        return message.get("result", {})

    def _process_message(self, message: Any) -> None:
        ticker = self._maybe_parse_book_ticker(message)
        if ticker is not None:
            self._ticker_cache[ticker.symbol] = ticker
            return

        order_event = self._maybe_parse_order_event(message)
        if order_event is not None:
            order_id = str(order_event.get("orderId") or "")
            client_order_id = str(order_event.get("clientOrderId") or "")
            if order_id:
                self._order_events_by_id[order_id] = order_event
            if client_order_id:
                self._order_events_by_client_id[client_order_id] = order_event

    def _find_order_event(
        self,
        *,
        order_id: str | None,
        client_order_id: str | None,
    ) -> dict[str, Any] | None:
        if order_id:
            event = self._order_events_by_id.get(str(order_id))
            if event is not None:
                return event
        if client_order_id:
            event = self._order_events_by_client_id.get(str(client_order_id))
            if event is not None:
                return event
        return None

    @staticmethod
    def _maybe_parse_book_ticker(message: Any) -> FastAPIBookTicker | None:
        if not isinstance(message, dict):
            return None
        symbol = message.get("s")
        if not isinstance(symbol, str):
            return None

        def to_float(value: Any) -> float | None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def to_int(value: Any) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return FastAPIBookTicker(
            symbol=symbol.lower(),
            bid=to_float(message.get("b")),
            bid_size=to_float(message.get("B")),
            ask=to_float(message.get("a")),
            ask_size=to_float(message.get("A")),
            update_id=to_int(message.get("u")),
            event_time_ns=to_int(message.get("E")),
        )

    @staticmethod
    def _maybe_parse_order_event(message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return None
        if "X" not in message or "i" not in message:
            return None

        status_map = {
            "NEW": "new",
            "OPEN": "open",
            "FILLED": "filled",
            "PARTIALLY_FILLED": "partially_filled",
            "CANCELED": "cancelled",
            "REJECTED": "rejected",
            "MODIFIED": "modified",
        }

        def to_float(value: Any) -> float | None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def to_int(value: Any) -> int | None:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

        original_qty = to_int(message.get("q")) or 0
        remaining_qty = to_int(message.get("z"))
        cumulative_or_last = to_int(message.get("Z"))
        executed_qty = cumulative_or_last
        if executed_qty is None and remaining_qty is not None:
            executed_qty = max(original_qty - remaining_qty, 0)

        outcome = str(message.get("O") or "").lower()
        side = str(message.get("S") or "").lower()
        status = status_map.get(str(message.get("X") or ""), str(message.get("X") or "").lower())

        return {
            "orderId": str(message.get("i") or ""),
            "clientOrderId": str(message.get("c") or ""),
            "symbol": str(message.get("s") or ""),
            "side": side,
            "outcome": outcome,
            "price": message.get("p"),
            "quantity": str(original_qty),
            "filledQuantity": str(executed_qty or 0),
            "avgExecutionPrice": message.get("L") or message.get("p"),
            "status": status,
            "isLive": status in {"new", "open", "partially_filled", "modified"},
            "isCancelled": status == "cancelled",
            "createdAt": message.get("T") or message.get("E"),
            "rawReason": message.get("r"),
        }


class GeminiExecutionClient:
    """Prediction-market client using Fast API for live order entry."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        base_url: str = PROD_BASE,
        fast_api_url: str | None = None,
        fast_api_spot_symbol: str = "btcusd",
        timeout: float = 10.0,
        dry_run: bool = False,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self.api_secret = (
            api_secret if api_secret is not None else os.environ.get("GEMINI_API_SECRET", "")
        )
        self.base_url = base_url.rstrip("/")
        self.fast_api_url = (fast_api_url or "").strip() or None
        self.fast_api_spot_symbol = fast_api_spot_symbol.lower()
        self.timeout = timeout
        self.dry_run = dry_run

        self._nonce_lock = threading.Lock()
        self._last_nonce_s = 0
        self._fast_api = self._make_fast_api_client(authenticated=False)
        self._private_fast_api = self._make_fast_api_client(authenticated=True)

    def _make_fast_api_client(self, *, authenticated: bool) -> GeminiFastAPIClient | None:
        if not self.fast_api_url:
            return None
        return GeminiFastAPIClient(
            self.fast_api_url,
            timeout=self.timeout,
            api_key=self.api_key if authenticated else "",
            api_secret=self.api_secret if authenticated else "",
        )

    # ── auth ────────────────────────────────────────────────────────────────

    def _next_nonce(self) -> str:
        with self._nonce_lock:
            now_s = int(time.time())
            if now_s <= self._last_nonce_s:
                now_s = self._last_nonce_s + 1
            self._last_nonce_s = now_s
            return str(now_s)

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
        if self._fast_api is not None:
            ticker = self._fast_api.get_book_ticker(self.fast_api_spot_symbol)
            if ticker is not None:
                if ticker.bid is not None and ticker.ask is not None:
                    return (ticker.bid + ticker.ask) / 2.0
                if ticker.bid is not None:
                    return ticker.bid
                if ticker.ask is not None:
                    return ticker.ask
        try:
            data = self._public_get("/v1/pubticker/BTCUSD")
        except ExecutionError:
            return None
        try:
            return float(data["last"])
        except (KeyError, TypeError, ValueError):
            return None

    def close(self) -> None:
        if self._fast_api is not None:
            self._fast_api.close()
        if self._private_fast_api is not None:
            self._private_fast_api.close()

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
        if self._private_fast_api is None:
            raise ExecutionError(
                "prediction markets order status is only available from Fast API order events",
                endpoint="/v1/prediction-markets/orders/status",
            )
        event = self._private_fast_api.get_order_event(order_id=order_id, timeout=0.0)
        if event is None:
            raise ExecutionError(
                f"no cached Fast API order event for {order_id}",
                endpoint="/v1/prediction-markets/orders/status",
            )
        return event

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
        time_in_force: str = "GTC",
    ) -> OrderResult:
        if side not in ("buy", "sell"):
            raise ValueError(f"invalid side {side!r}")
        if outcome not in ("yes", "no"):
            raise ValueError(f"invalid outcome {outcome!r}")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if not (0.0 < price < 1.0):
            raise ValueError(f"price {price} outside (0,1)")

        fast_tif = self._normalise_fast_api_time_in_force(time_in_force)
        ws_payload = {
            "symbol": instrument,
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": fast_tif,
            "price": f"{price:.4f}",
            "quantity": str(quantity),
            "eventOutcome": outcome.upper(),
            "clientOrderId": client_order_id,
        }
        rest_payload = {
            "symbol": instrument,
            "orderType": "limit",
            "side": side,
            "outcome": outcome,
            "quantity": str(quantity),
            "price": f"{price:.4f}",
            "timeInForce": fast_tif,
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
                raw=ws_payload,
            )

        if self._private_fast_api is not None:
            self._private_fast_api.subscribe(["orders@account"])
            result = self._private_fast_api.call("order.place", ws_payload)
            merged = self._merge_fast_api_order_state(
                result=result,
                event=self._private_fast_api.get_order_event(
                    client_order_id=client_order_id,
                    order_id=str(result.get("orderId") or ""),
                    timeout=min(self.timeout, 1.0),
                ),
            )
            return self.normalise_order(
                merged,
                requested_quantity=quantity,
                requested_price=price,
                client_order_id=client_order_id,
                instrument=instrument,
                side=side,
                outcome=outcome,
            )

        data = self._private_post("/v1/prediction-markets/order", rest_payload)
        if not isinstance(data, dict):
            raise ExecutionError(
                "order placement response was not a dict",
                endpoint="/v1/prediction-markets/order",
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
        if self._private_fast_api is not None:
            result = self._private_fast_api.call(
                "order.cancel",
                {"orderId": str(order_id)},
            )
            return dict(result) if isinstance(result, dict) else {"result": result}
        return self._private_post("/v1/prediction-markets/order/cancel", {"orderId": order_id})

    @staticmethod
    def _normalise_fast_api_time_in_force(time_in_force: str) -> str:
        normalised = str(time_in_force or "").strip().upper()
        aliases = {
            "GOOD_TIL_CANCELLED": "GTC",
            "GOOD-TIL-CANCELLED": "GTC",
        }
        normalised = aliases.get(normalised, normalised)
        if normalised != "GTC":
            raise ValueError(
                f"unsupported Fast API time_in_force {time_in_force!r}; docs in gemini-prediction-markets-llms.md specify GTC"
            )
        return normalised

    @staticmethod
    def _merge_fast_api_order_state(
        *,
        result: Any,
        event: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base = dict(result) if isinstance(result, dict) else {}
        if event:
            base.update(event)
        if "clientOrderId" not in base and event and event.get("clientOrderId"):
            base["clientOrderId"] = event["clientOrderId"]
        return base

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

        def to_timestamp_ms(value):
            numeric = to_int(value, default=0)
            if numeric:
                if numeric > 10_000_000_000_000:
                    return int(numeric / 1_000_000)
                return numeric
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return int(parsed.timestamp() * 1000)
                except ValueError:
                    return None
            return None

        is_live = bool(first("is_live", "isLive", default=False))
        is_cancelled = bool(first("is_cancelled", "isCancelled", default=False))
        status = str(first("status", "X", default="unknown")).lower()

        return OrderResult(
            order_id=str(first("order_id", "orderId", "id", default="")),
            client_order_id=str(
                first("client_order_id", "clientOrderId", default=client_order_id or "")
            ),
            instrument=str(first("symbol", "instrumentSymbol", default=instrument or "")),
            side=str(first("side", "S", default=side or "")).lower(),
            outcome=str(first("outcome", "eventOutcome", "O", default=outcome or "")).lower(),
            requested_quantity=to_int(
                first(
                    "original_amount",
                    "originalAmount",
                    "quantity",
                    "q",
                    default=requested_quantity or 0,
                )
            ),
            requested_price=to_float(
                first("price", "p", default=requested_price if requested_price is not None else 0.0)
            ),
            filled_quantity=to_int(
                first("executed_amount", "executedAmount", "filledQuantity", "Z", default=0)
            ),
            avg_execution_price=to_float(
                first("avg_execution_price", "avgExecutionPrice", "L", default=0.0)
            ),
            fee=to_float(first("fee", default=0.0)),
            status=status,
            is_live=is_live or status == "open",
            is_cancelled=is_cancelled or status == "cancelled",
            created_at_ms=to_timestamp_ms(
                first("timestampms", "timestampMs", "createdAt", "T", "E", default=0)
            ),
            raw=raw,
        )
