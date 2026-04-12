from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from strategy.execution import (
    GeminiExecutionClient,
    OrderResult,
    ExecutionError,
    FastAPIBookTicker,
)


class SignAndNonceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GeminiExecutionClient(
            api_key="account-test", api_secret="secret-test"
        )

    def test_sign_request_includes_required_headers(self) -> None:
        headers = self.client._sign("/v1/prediction-markets/positions", {})
        self.assertEqual(headers["X-GEMINI-APIKEY"], "account-test")
        self.assertIn("X-GEMINI-PAYLOAD", headers)
        self.assertIn("X-GEMINI-SIGNATURE", headers)
        self.assertEqual(headers["Content-Type"], "text/plain")

    def test_nonce_is_monotonic(self) -> None:
        nonces = [int(self.client._next_nonce()) for _ in range(50)]
        self.assertEqual(nonces, sorted(nonces))
        self.assertEqual(len(set(nonces)), len(nonces))

    def test_sign_without_credentials_raises(self) -> None:
        client = GeminiExecutionClient(api_key="", api_secret="")
        with self.assertRaises(ExecutionError):
            client._sign("/v1/prediction-markets/positions", {})

    def test_fast_api_auth_headers_use_second_nonce(self) -> None:
        self.client.fast_api_url = "wss://wsapi.fast.gemini.com"
        fast_api = self.client._make_fast_api_client(authenticated=True)
        assert fast_api is not None
        headers = fast_api._auth_headers()
        self.assertEqual(headers["X-GEMINI-APIKEY"], "account-test")
        self.assertIn("X-GEMINI-NONCE", headers)
        self.assertIn("X-GEMINI-PAYLOAD", headers)
        self.assertIn("X-GEMINI-SIGNATURE", headers)


class NormaliseOrderTests(unittest.TestCase):
    def test_camelcase_payload(self) -> None:
        raw = {
            "orderId": "abc-1",
            "clientOrderId": "client-1",
            "instrumentSymbol": "GEMI-BTC2604111200-HI100000",
            "side": "buy",
            "outcome": "yes",
            "originalAmount": "10",
            "executedAmount": "7",
            "avgExecutionPrice": "0.4321",
            "fee": "0.05",
            "price": "0.4400",
            "status": "filled",
            "isLive": False,
            "isCancelled": True,
            "timestampms": 1700000000000,
        }
        result = GeminiExecutionClient.normalise_order(raw)
        self.assertEqual(result.order_id, "abc-1")
        self.assertEqual(result.client_order_id, "client-1")
        self.assertEqual(result.instrument, "GEMI-BTC2604111200-HI100000")
        self.assertEqual(result.requested_quantity, 10)
        self.assertEqual(result.filled_quantity, 7)
        self.assertAlmostEqual(result.avg_execution_price, 0.4321)
        self.assertAlmostEqual(result.fee, 0.05)
        self.assertEqual(result.status, "filled")
        self.assertFalse(result.is_live)
        self.assertTrue(result.is_cancelled)

    def test_snake_case_payload(self) -> None:
        raw = {
            "order_id": "abc-2",
            "client_order_id": "client-2",
            "symbol": "GEMI-BTC2604120000-HI95000",
            "side": "sell",
            "outcome": "yes",
            "original_amount": "5",
            "executed_amount": "0",
            "avg_execution_price": "0",
            "status": "cancelled",
            "is_cancelled": True,
        }
        result = GeminiExecutionClient.normalise_order(raw)
        self.assertEqual(result.order_id, "abc-2")
        self.assertEqual(result.requested_quantity, 5)
        self.assertEqual(result.filled_quantity, 0)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.is_cancelled)

    def test_missing_fields_use_defaults(self) -> None:
        result = GeminiExecutionClient.normalise_order(
            {},
            requested_quantity=3,
            requested_price=0.55,
            client_order_id="fallback",
            instrument="GEMI-BTCxxx",
            side="buy",
            outcome="yes",
        )
        self.assertEqual(result.requested_quantity, 3)
        self.assertEqual(result.client_order_id, "fallback")
        self.assertEqual(result.instrument, "GEMI-BTCxxx")
        self.assertEqual(result.filled_quantity, 0)
        self.assertEqual(result.status, "unknown")

    def test_prediction_market_payload(self) -> None:
        raw = {
            "orderId": 12345678,
            "clientOrderId": None,
            "status": "open",
            "symbol": "GEMI-BTC2604120000-HI95000",
            "side": "buy",
            "outcome": "yes",
            "orderType": "limit",
            "quantity": "5",
            "filledQuantity": "2",
            "remainingQuantity": "3",
            "price": "0.4200",
            "avgExecutionPrice": "0.4100",
            "createdAt": "2024-08-25T15:00:00Z",
        }
        result = GeminiExecutionClient.normalise_order(raw)
        self.assertEqual(result.order_id, "12345678")
        self.assertEqual(result.requested_quantity, 5)
        self.assertEqual(result.filled_quantity, 2)
        self.assertEqual(result.status, "open")
        self.assertTrue(result.is_live)
        self.assertFalse(result.is_cancelled)
        self.assertIsNotNone(result.created_at_ms)


class PlaceOrderValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GeminiExecutionClient(
            api_key="account-test", api_secret="secret-test", dry_run=True
        )

    def test_dry_run_returns_synthetic_order(self) -> None:
        result = self.client.place_order(
            instrument="GEMI-BTC2604110000-HI100000",
            side="buy",
            outcome="yes",
            quantity=2,
            price=0.45,
            client_order_id="dry-1",
        )
        self.assertIsInstance(result, OrderResult)
        self.assertEqual(result.order_id, "dry-run")
        self.assertEqual(result.requested_quantity, 2)
        self.assertEqual(result.filled_quantity, 0)
        self.assertTrue(result.is_cancelled)

    def test_invalid_side_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.client.place_order(
                instrument="GEMI-BTC2604110000-HI100000",
                side="long",
                outcome="yes",
                quantity=1,
                price=0.4,
                client_order_id="x",
            )

    def test_invalid_price_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.client.place_order(
                instrument="GEMI-BTC2604110000-HI100000",
                side="buy",
                outcome="yes",
                quantity=1,
                price=1.2,
                client_order_id="x",
            )

    def test_invalid_quantity_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.client.place_order(
                instrument="GEMI-BTC2604110000-HI100000",
                side="buy",
                outcome="yes",
                quantity=0,
                price=0.4,
                client_order_id="x",
            )

    def test_invalid_fast_api_time_in_force_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.client.place_order(
                instrument="GEMI-BTC2604110000-HI100000",
                side="buy",
                outcome="yes",
                quantity=1,
                price=0.4,
                client_order_id="x",
                time_in_force="IOC",
            )

    def test_place_order_uses_fast_api_when_configured(self) -> None:
        client = GeminiExecutionClient(
            api_key="account-test",
            api_secret="secret-test",
            fast_api_url="wss://wsapi.fast.gemini.com",
            dry_run=False,
        )

        class StubPrivateFastAPI:
            def __init__(self):
                self.subscribed = []
                self.called = []

            def subscribe(self, streams):
                self.subscribed.append(list(streams))

            def call(self, method, params):
                self.called.append((method, dict(params)))
                return {"orderId": "73797746498585286", "status": "open"}

            def get_order_event(self, **kwargs):
                return {
                    "orderId": "73797746498585286",
                    "clientOrderId": kwargs.get("client_order_id", ""),
                    "symbol": "GEMI-BTC2604110000-HI100000",
                    "side": "buy",
                    "outcome": "yes",
                    "price": "0.4000",
                    "quantity": "1",
                    "filledQuantity": "0",
                    "status": "open",
                    "createdAt": "2024-08-25T15:00:00Z",
                }

            def close(self):
                return None

        stub = StubPrivateFastAPI()
        client._private_fast_api = stub
        result = client.place_order(
            instrument="GEMI-BTC2604110000-HI100000",
            side="buy",
            outcome="yes",
            quantity=1,
            price=0.4,
            client_order_id="cid-1",
            time_in_force="GTC",
        )

        self.assertEqual(stub.subscribed, [["orders@account"]])
        self.assertEqual(stub.called[0][0], "order.place")
        self.assertEqual(stub.called[0][1]["side"], "BUY")
        self.assertEqual(stub.called[0][1]["eventOutcome"], "YES")
        self.assertEqual(stub.called[0][1]["type"], "LIMIT")
        self.assertEqual(result.order_id, "73797746498585286")
        self.assertEqual(result.client_order_id, "cid-1")
        self.assertEqual(result.status, "open")


class ExtractListTests(unittest.TestCase):
    def test_list_passthrough(self) -> None:
        self.assertEqual(
            GeminiExecutionClient._extract_list([{"a": 1}], "orders"),
            [{"a": 1}],
        )

    def test_dict_with_named_key(self) -> None:
        self.assertEqual(
            GeminiExecutionClient._extract_list({"orders": [{"a": 1}]}, "orders"),
            [{"a": 1}],
        )

    def test_dict_with_data_key(self) -> None:
        self.assertEqual(
            GeminiExecutionClient._extract_list({"data": [{"a": 1}]}, "orders"),
            [{"a": 1}],
        )

    def test_empty_default(self) -> None:
        self.assertEqual(GeminiExecutionClient._extract_list(None, "orders"), [])


class FastAPISpotTests(unittest.TestCase):
    def test_get_spot_uses_fast_api_midpoint(self) -> None:
        client = GeminiExecutionClient(
            api_key="account-test",
            api_secret="secret-test",
            fast_api_url="ws://example.fast.gemini.com",
        )

        class StubFastAPI:
            def get_book_ticker(self, symbol: str) -> FastAPIBookTicker:
                self.symbol = symbol
                return FastAPIBookTicker(
                    symbol="btcusd",
                    bid=100000.0,
                    bid_size=1.0,
                    ask=100010.0,
                    ask_size=2.0,
                    update_id=1,
                    event_time_ns=2,
                )

            def close(self) -> None:
                return None

        client._fast_api = StubFastAPI()
        self.assertAlmostEqual(client.get_spot(), 100005.0)

    def test_get_spot_falls_back_to_rest_when_fast_api_disabled(self) -> None:
        client = GeminiExecutionClient(api_key="account-test", api_secret="secret-test")
        client._public_get = lambda path: {"last": "99999.5"}  # type: ignore[method-assign]
        self.assertAlmostEqual(client.get_spot(), 99999.5)


if __name__ == "__main__":
    unittest.main()
