#!/usr/bin/env python3
"""
Gemini Prediction Markets REST API test script.

Tests authentication, account state, and market data endpoints.
Does NOT place or cancel any orders.

Setup (before running):
  1. Create a REST API key in the Gemini UI (Settings → API Keys).
     Required permissions: Fund Management, Trading (NewOrder + CancelOrder).
     NOTE: The RSA PEM file in keys/ is for FIX protocol only — it does NOT
     work for REST authentication. REST uses HMAC-SHA384 with a key+secret pair.

  2. Accept the Prediction Markets Terms of Service in the Gemini UI.
     Without this, private PM endpoints return 403 Forbidden.

  3. Set environment variables:
       export GEMINI_API_KEY="account-xxxxxxxxxxxxxxxxxxxx"
       export GEMINI_API_SECRET="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

  4. Run: python src/test_gemini_api.py
"""

import hmac
import hashlib
import base64
import json
import time
import urllib.request
import urllib.error
import os
import sys

# ── config ────────────────────────────────────────────────────────────────────

GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_SECRET = os.environ.get("GEMINI_API_SECRET", "")

BASE = "https://api.gemini.com"
# BASE = "https://api.sandbox.gemini.com"  # uncomment to use sandbox


# ── auth ──────────────────────────────────────────────────────────────────────

def sign_request(endpoint: str, payload: dict) -> dict:
    """
    Build headers for a Gemini private REST request.

    Auth scheme (HMAC-SHA384):
      1. Add 'request' (path) and 'nonce' (seconds timestamp) to payload dict.
      2. JSON-encode, then base64-encode → X-GEMINI-PAYLOAD.
      3. HMAC-SHA384(base64_payload, key=api_secret).hexdigest() → X-GEMINI-SIGNATURE.
      4. X-GEMINI-APIKEY is the plain API key string.
    """
    payload["request"] = endpoint
    payload["nonce"]   = str(int(time.time()))

    encoded      = json.dumps(payload).encode()
    b64_payload  = base64.b64encode(encoded).decode()
    signature    = hmac.new(
        GEMINI_API_SECRET.encode(),
        b64_payload.encode(),
        hashlib.sha384,
    ).hexdigest()

    return {
        "Content-Type":      "text/plain",
        "Content-Length":    "0",
        "Cache-Control":     "no-cache",
        "X-GEMINI-APIKEY":   GEMINI_API_KEY,
        "X-GEMINI-PAYLOAD":  b64_payload,
        "X-GEMINI-SIGNATURE": signature,
    }


def private_post(endpoint: str, payload: dict | None = None):
    """Authenticated POST. Returns (data, error_str)."""
    if not GEMINI_API_KEY or not GEMINI_API_SECRET:
        return None, "GEMINI_API_KEY / GEMINI_API_SECRET not set"

    body    = payload or {}
    headers = sign_request(endpoint, body)
    req     = urllib.request.Request(
        f"{BASE}{endpoint}", data=b"", headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()}"
    except Exception as e:
        return None, str(e)


def public_get(url: str):
    """Unauthenticated GET. Returns (data, error_str)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()}"
    except Exception as e:
        return None, str(e)


# ── test helpers ──────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def ok(label: str, value=None):
    suffix = f"  →  {value}" if value is not None else ""
    print(f"  [OK]  {label}{suffix}")


def fail(label: str, error: str):
    print(f"  [FAIL] {label}: {error}", file=sys.stderr)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_public_connectivity():
    section("1. Public connectivity — active BTC events")
    data, err = public_get(
        f"{BASE}/v1/prediction-markets/events?limit=10&search=BTC&status=active"
    )
    if err:
        fail("fetch active events", err)
        return []

    events = data.get("data", [])
    ok(f"active events returned", len(events))
    for e in events:
        ticker    = e.get("ticker")
        n_contr   = len(e.get("contracts", []))
        expiry    = e.get("expiryDate", "?")
        volume    = e.get("volume")
        liquidity = e.get("liquidity")
        # Check if contractOrderbooks field is present
        ob        = e.get("contractOrderbooks")
        print(f"    {ticker}  contracts={n_contr}  expiry={expiry}  "
              f"vol={volume}  liq={liquidity}  orderbooks={'yes' if ob else 'no'}")

        # Inspect first contract's fields
        contracts = e.get("contracts", [])
        if contracts:
            c = contracts[0]
            print(f"      sample contract keys: {list(c.keys())}")
            prices = c.get("prices", {})
            print(f"      prices keys: {list(prices.keys())}  values: {prices}")
    return events


def test_spot_ticker():
    section("2. Public — BTC/USD spot ticker")
    data, err = public_get(f"{BASE}/v1/pubticker/BTCUSD")
    if err:
        fail("spot ticker", err)
        return
    ok("bid",  data.get("bid"))
    ok("ask",  data.get("ask"))
    ok("last", data.get("last"))


def test_auth_positions():
    section("3. Private — get prediction market positions")
    data, err = private_post("/v1/prediction-markets/positions")
    if err:
        fail("positions", err)
        return
    positions = data if isinstance(data, list) else data.get("positions", data)
    ok(f"positions returned", len(positions) if isinstance(positions, list) else "?")
    if isinstance(positions, list) and positions:
        print(f"  Sample position keys: {list(positions[0].keys())}")
        for p in positions[:3]:
            print(f"    {p}")
    else:
        print("  (no open positions)")


def test_auth_active_orders():
    section("4. Private — get active prediction market orders")
    data, err = private_post("/v1/prediction-markets/orders/active", {"limit": 10})
    if err:
        fail("active orders", err)
        return
    orders = data if isinstance(data, list) else data.get("orders", data)
    ok(f"active orders returned", len(orders) if isinstance(orders, list) else "?")
    if isinstance(orders, list) and orders:
        print(f"  Sample order keys: {list(orders[0].keys())}")
        for o in orders[:3]:
            print(f"    {o}")
    else:
        print("  (no active orders)")


def test_auth_order_history():
    section("5. Private — get filled order history (last 10)")
    data, err = private_post(
        "/v1/prediction-markets/orders/history", {"status": "filled", "limit": 10}
    )
    if err:
        fail("order history", err)
        return
    orders = data if isinstance(data, list) else data.get("orders", data)
    ok(f"filled orders returned", len(orders) if isinstance(orders, list) else "?")
    if isinstance(orders, list) and orders:
        print(f"  Sample order keys: {list(orders[0].keys())}")
        for o in orders[:3]:
            print(f"    symbol={o.get('symbol')}  side={o.get('side')}  "
                  f"outcome={o.get('outcome')}  qty={o.get('filledQuantity')}  "
                  f"avgPrice={o.get('avgExecutionPrice')}")


def test_volume_metrics(events: list):
    section("6. Private — volume metrics for first active event")
    if not events:
        print("  (skipped — no active events)")
        return

    ticker = events[0].get("ticker")
    data, err = private_post(
        "/v1/prediction-markets/metrics/volume", {"eventTicker": ticker}
    )
    if err:
        fail(f"volume metrics for {ticker}", err)
        return

    ok(f"volume metrics for {ticker}")
    metrics = data if isinstance(data, list) else data.get("metrics", data)
    if isinstance(metrics, list) and metrics:
        print(f"  Sample metric keys: {list(metrics[0].keys())}")
        for m in metrics[:5]:
            print(f"    {m}")
    else:
        print(f"  Raw response: {data}")


def test_order_book_via_standard_api(events: list):
    """
    The PM docs say to use the standard market data APIs with the instrumentSymbol.
    Test this with the public order book endpoint.
    """
    section("7. Public — order book via standard market data API")
    if not events:
        print("  (skipped — no active events)")
        return

    contracts = events[0].get("contracts", [])
    if not contracts:
        print("  (skipped — no contracts in first event)")
        return

    symbol = contracts[0].get("instrumentSymbol", "")
    if not symbol:
        return

    # Standard order book endpoint
    url = f"{BASE}/v1/book/{symbol}?limit_bids=5&limit_asks=5"
    data, err = public_get(url)
    if err:
        fail(f"order book for {symbol}", err)
        return

    ok(f"order book for {symbol}")
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    print(f"  bids ({len(bids)}): {bids[:3]}")
    print(f"  asks ({len(asks)}): {asks[:3]}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("\nGemini Prediction Markets API Test")
    print(f"Base URL: {BASE}")
    print(f"API key set: {'yes' if GEMINI_API_KEY else 'NO — set GEMINI_API_KEY env var'}")
    print(f"API secret set: {'yes' if GEMINI_API_SECRET else 'NO — set GEMINI_API_SECRET env var'}")

    events = test_public_connectivity()
    test_spot_ticker()
    test_auth_positions()
    test_auth_active_orders()
    test_auth_order_history()
    test_volume_metrics(events)
    test_order_book_via_standard_api(events)

    print("\n" + "="*60)
    print("  Done.")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
