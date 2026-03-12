"""
Collect Gemini spot BTC/USD price data across all available timeframes.

Gemini's candle API returns a fixed rolling window — no historical pagination.
Coverage limits (as of collection date):
  1m   ->  last ~24 hours
  5m   ->  last ~7 days
  15m  ->  last ~14 days
  30m  ->  last ~40 days
  1hr  ->  last ~59 days
  6hr  ->  last ~90 days
  1day ->  last ~365 days

A composite "best resolution" file is also built that uses the finest available
timeframe for each timestamp (1m where available, falling back to 5m, 15m, etc.)
"""

import urllib.request
import json
import csv
import os
import datetime

BASE   = "https://api.gemini.com"
SYMBOL = "BTCUSD"
OUT_DIR = "data/gemini_spot"

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1hr", "6hr", "1day"]

# Resolution in minutes for each timeframe (used when building composite)
TF_MINUTES = {
    "1m":   1,
    "5m":   5,
    "15m":  15,
    "30m":  30,
    "1hr":  60,
    "6hr":  360,
    "1day": 1440,
}

os.makedirs(OUT_DIR, exist_ok=True)


def fetch_candles(symbol, timeframe):
    url = f"{BASE}/v2/candles/{symbol}/{timeframe}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())  # [[ts_ms, open, high, low, close, volume], ...]


def candles_to_rows(candles):
    """Sort oldest-first and return as list of dicts."""
    rows = []
    for c in sorted(candles, key=lambda x: x[0]):
        ts_ms = c[0]
        dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
        rows.append({
            "timestamp_ms":  ts_ms,
            "datetime_utc":  dt.strftime("%Y-%m-%d %H:%M:%S"),
            "open":          c[1],
            "high":          c[2],
            "low":           c[3],
            "close":         c[4],
            "volume":        c[5],
        })
    return rows


def main():
    print(f"=== Gemini Spot {SYMBOL} Data Collector ===\n")

    all_by_tf = {}

    # 1. Fetch each timeframe
    for tf in TIMEFRAMES:
        print(f"  Fetching {tf}...", end=" ", flush=True)
        try:
            raw = fetch_candles(SYMBOL, tf)
            rows = candles_to_rows(raw)
            all_by_tf[tf] = rows

            # Write per-timeframe file
            path = os.path.join(OUT_DIR, f"BTCUSD_{tf}.csv")
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)

            oldest = rows[0]["datetime_utc"]
            newest = rows[-1]["datetime_utc"]
            print(f"{len(rows):5d} candles | {oldest} -> {newest}")
        except Exception as e:
            print(f"ERROR: {e}")
            all_by_tf[tf] = []

    # 2. Build composite: finest resolution per timestamp
    # Strategy: collect all candles, deduplicate by timestamp keeping the
    # finest-resolution source. For each timestamp bucket, align to nearest
    # coarser timeframe if exact match missing.
    print("\nBuilding composite best-resolution dataset...")

    # Collect all rows tagged with their resolution in minutes
    seen = {}  # timestamp_ms -> (resolution_minutes, row)

    for tf in TIMEFRAMES:  # ordered coarse-to-fine (reversed later)
        res = TF_MINUTES[tf]
        for row in all_by_tf.get(tf, []):
            ts = row["timestamp_ms"]
            if ts not in seen or res < seen[ts][0]:
                seen[ts] = (res, row)

    composite = [row for _, row in sorted(seen.values(), key=lambda x: x[1]["timestamp_ms"])]

    # Add a column indicating source resolution
    for (res, row) in seen.values():
        row["source_tf_min"] = res

    composite_path = os.path.join(OUT_DIR, "BTCUSD_composite.csv")
    fieldnames = ["timestamp_ms", "datetime_utc", "open", "high", "low", "close", "volume", "source_tf_min"]
    with open(composite_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for _, row in sorted(seen.values(), key=lambda x: x[1]["timestamp_ms"]):
            w.writerow(row)

    # Summary
    print(f"\n=== Summary ===")
    for tf in TIMEFRAMES:
        rows = all_by_tf.get(tf, [])
        if rows:
            print(f"  {tf:5s}: {len(rows):5d} rows | {rows[0]['datetime_utc']} -> {rows[-1]['datetime_utc']}")
        else:
            print(f"  {tf:5s}: no data")

    print(f"\n  Composite: {len(seen):5d} unique timestamps -> {composite_path}")

    # Coverage note
    print("""
Coverage note:
  Gemini's API does not support historical pagination — each timeframe
  returns only the most recent N candles (fixed rolling window).

  For prediction market data going back to 2025-12-15:
  - 6hr candles cover from ~2025-12-03 (closest available)
  - 1day candles cover the full year back to ~2025-03-03

  For higher-frequency historical spot (for backtesting Dec 2025 / Jan 2026
  prediction market trades at sub-hour resolution), supplement with
  BTC/USDT from Binance global (api.binance.com) which has equivalent
  pricing and far deeper candle history.
""")


if __name__ == "__main__":
    main()
