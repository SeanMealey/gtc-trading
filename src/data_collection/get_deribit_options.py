"""
Collect BTC options chain from Deribit public API (no auth required).

Fetches all active BTC options with:
  - instrument metadata: strike, expiry, option_type
  - market data: mark_iv, mark_price, bid/ask, underlying_price, open_interest

Output: data/deribit/btc_options_chain.csv
        data/deribit/btc_options_chain_{YYYYMMDD_HHMMSS}.csv  (timestamped snapshot)
"""

import csv
import json
import os
import time
from datetime import datetime, timezone
from urllib.request import urlopen
from urllib.error import URLError

BASE_URL = "https://www.deribit.com/api/v2/public"
OUT_DIR = os.path.join(os.path.dirname(__file__), "../../data/deribit")

COLUMNS = [
    "instrument_name",
    "option_type",       # call / put
    "strike",
    "expiry_ts_ms",      # expiration timestamp (ms UTC)
    "expiry_dt",         # human-readable expiry e.g. 2026-03-28T08:00:00Z
    "tte_years",         # time to expiry in years from snapshot time
    "underlying_price",  # Deribit forward price at that expiry
    "mark_iv",           # Deribit Black-Scholes implied vol (%)
    "mark_price_btc",    # mark price in BTC
    "bid_price_btc",
    "ask_price_btc",
    "mid_price_btc",
    "open_interest",     # in contracts (1 contract = 1 BTC)
    "volume_usd_24h",
    "snapshot_ts",       # UTC timestamp of this snapshot
]


def fetch(endpoint: str, params: dict = None) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    with urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def get_instruments() -> dict[str, dict]:
    """Return dict keyed by instrument_name with metadata."""
    data = fetch("get_instruments", {"currency": "BTC", "kind": "option", "expired": "false"})
    instruments = {}
    for inst in data.get("result", []):
        instruments[inst["instrument_name"]] = {
            "option_type": inst["option_type"],   # "call" or "put"
            "strike": inst["strike"],
            "expiry_ts_ms": inst["expiration_timestamp"],
        }
    return instruments


def get_book_summaries() -> dict[str, dict]:
    """Return dict keyed by instrument_name with market data."""
    data = fetch("get_book_summary_by_currency", {"currency": "BTC", "kind": "option"})
    summaries = {}
    for item in data.get("result", []):
        name = item["instrument_name"]
        summaries[name] = {
            "underlying_price": item.get("underlying_price"),
            "mark_iv":          item.get("mark_iv"),       # in %, e.g. 49.6
            "mark_price_btc":   item.get("mark_price"),
            "bid_price_btc":    item.get("bid_price"),
            "ask_price_btc":    item.get("ask_price"),
            "mid_price_btc":    item.get("mid_price"),
            "open_interest":    item.get("open_interest", 0),
            "volume_usd_24h":   item.get("volume_usd", 0),
        }
    return summaries


def build_rows(instruments: dict, summaries: dict, snapshot_ts: str, now_ms: float) -> list[dict]:
    rows = []
    for name, meta in instruments.items():
        mkt = summaries.get(name, {})

        expiry_ms = meta["expiry_ts_ms"]
        expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        tte_years = max(0.0, (expiry_ms - now_ms) / 1000 / 86400 / 365.25)

        mark_iv = mkt.get("mark_iv")
        # Skip instruments with no market activity at all
        if mark_iv is None and mkt.get("open_interest", 0) == 0:
            continue

        rows.append({
            "instrument_name":  name,
            "option_type":      meta["option_type"],
            "strike":           meta["strike"],
            "expiry_ts_ms":     expiry_ms,
            "expiry_dt":        expiry_dt,
            "tte_years":        round(tte_years, 8),
            "underlying_price": mkt.get("underlying_price"),
            "mark_iv":          mark_iv,
            "mark_price_btc":   mkt.get("mark_price_btc"),
            "bid_price_btc":    mkt.get("bid_price_btc"),
            "ask_price_btc":    mkt.get("ask_price_btc"),
            "mid_price_btc":    mkt.get("mid_price_btc"),
            "open_interest":    mkt.get("open_interest", 0),
            "volume_usd_24h":   mkt.get("volume_usd_24h", 0),
            "snapshot_ts":      snapshot_ts,
        })

    rows.sort(key=lambda r: (r["expiry_ts_ms"], r["strike"], r["option_type"]))
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    now_ms = time.time() * 1000
    snapshot_ts = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    stamp = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    print(f"Fetching Deribit BTC instruments...")
    instruments = get_instruments()
    print(f"  {len(instruments)} instruments found")

    print(f"Fetching book summaries...")
    summaries = get_book_summaries()
    print(f"  {len(summaries)} summaries returned")

    rows = build_rows(instruments, summaries, snapshot_ts, now_ms)
    print(f"  {len(rows)} rows after filtering (mark_iv or OI > 0)")

    # Print a quick summary
    expiries = sorted(set(r["expiry_dt"] for r in rows))
    calls = sum(1 for r in rows if r["option_type"] == "call")
    puts  = sum(1 for r in rows if r["option_type"] == "put")
    liquid = sum(1 for r in rows if r["open_interest"] and float(r["open_interest"]) > 10)
    print(f"\n  Expiries ({len(expiries)}):")
    for e in expiries[:12]:
        n = sum(1 for r in rows if r["expiry_dt"] == e)
        oi = sum(float(r["open_interest"] or 0) for r in rows if r["expiry_dt"] == e)
        print(f"    {e}  {n:3d} instruments  OI={oi:.0f} BTC")
    if len(expiries) > 12:
        print(f"    ... and {len(expiries)-12} more expiries")
    print(f"\n  calls={calls}  puts={puts}  liquid(OI>10)={liquid}")

    latest_path    = os.path.join(OUT_DIR, "btc_options_chain.csv")
    snapshot_path  = os.path.join(OUT_DIR, f"btc_options_chain_{stamp}.csv")

    write_csv(rows, latest_path)
    write_csv(rows, snapshot_path)
    print(f"\nSaved:\n  {latest_path}\n  {snapshot_path}")


if __name__ == "__main__":
    main()
