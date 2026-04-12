"""
Fast API websocket smoke test for Gemini low-latency connectivity.

Usage:
  GEMINI_FAST_API_URL=ws://2e4e907b.fast.gemini.com \
  python src/test_gemini_fast_api.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time

from websockets.sync.client import connect


DEFAULT_URL = "ws://2e4e907b.fast.gemini.com"
DEFAULT_STREAM = "btcusd@bookTicker"


def auth_headers() -> dict[str, str] | None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    api_secret = os.environ.get("GEMINI_API_SECRET", "").strip()
    if not api_key and not api_secret:
        return None
    if not api_key or not api_secret:
        raise SystemExit("set both GEMINI_API_KEY and GEMINI_API_SECRET for authenticated Fast API tests")

    nonce = str(int(time.time()))
    payload = base64.b64encode(nonce.encode()).decode()
    signature = hmac.new(
        api_secret.encode(),
        payload.encode(),
        hashlib.sha384,
    ).hexdigest()
    return {
        "X-GEMINI-APIKEY": api_key,
        "X-GEMINI-NONCE": nonce,
        "X-GEMINI-PAYLOAD": payload,
        "X-GEMINI-SIGNATURE": signature,
    }


def main() -> int:
    url = os.environ.get("GEMINI_FAST_API_URL", DEFAULT_URL).strip() or DEFAULT_URL
    stream = os.environ.get("GEMINI_FAST_API_STREAM", DEFAULT_STREAM).strip() or DEFAULT_STREAM

    payload = {
        "id": "1",
        "method": "subscribe",
        "params": [stream],
    }
    headers = auth_headers()

    print(f"connecting to {url}")
    print(f"subscribing to {stream}")
    if headers:
        print("using authenticated websocket handshake")
    with connect(
        url,
        additional_headers=headers,
        open_timeout=10,
        close_timeout=10,
        proxy=None,
    ) as websocket:
        websocket.send(json.dumps(payload))
        message = websocket.recv(timeout=10)

    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
