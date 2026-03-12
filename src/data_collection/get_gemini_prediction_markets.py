"""
Collect historical data for Gemini BTC prediction markets.
Markets follow the format: GEMI-BTC{YYMMDDHHII}-HI{PRICE}
These are binary options that resolve to 1 if BTC > PRICE at expiry, else 0.
Uses parallel workers to collect candle + trade data efficiently.
"""

import urllib.request
import urllib.error
import json
import csv
import os
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

BASE    = "https://api.gemini.com"
OUT_DIR = "data/gemini_prediction_markets"
CANDLES_DIR = os.path.join(OUT_DIR, "candles")
TRADES_DIR  = os.path.join(OUT_DIR, "trades")

os.makedirs(CANDLES_DIR, exist_ok=True)
os.makedirs(TRADES_DIR,  exist_ok=True)

WORKERS = 20       # parallel threads
TIMEFRAME = "5m"   # candle resolution


# ── helpers ──────────────────────────────────────────────────────────────────

def get(url, retries=3):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt)
            elif e.code in (400, 404):
                return None
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None


def fetch_all_btc_events():
    """Paginate through all BTC events across all statuses."""
    events = []
    for st in ["active", "closed", "settled"]:
        offset = 0
        while True:
            url = (
                f"{BASE}/v1/prediction-markets/events"
                f"?limit=100&search=BTC&status={st}&offset={offset}"
            )
            data = get(url)
            if not data:
                break
            batch = data.get("data", [])
            events.extend(batch)
            print(f"  [{st}] {offset + len(batch)} events...")
            if len(batch) < 100:
                break
            offset += 100
    return events


def collect_instrument(symbol):
    """Fetch candles + trades for one instrument. Returns (symbol, n_candles, n_trades)."""
    candle_path = os.path.join(CANDLES_DIR, f"{symbol}.csv")
    trade_path  = os.path.join(TRADES_DIR,  f"{symbol}.csv")

    # Candles
    n_candles = 0
    if not os.path.exists(candle_path):
        candles = get(f"{BASE}/v2/candles/{symbol}/{TIMEFRAME}") or []
        if candles:
            with open(candle_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["timestamp_ms", "open", "high", "low", "close", "volume"])
                w.writerows(sorted(candles, key=lambda x: x[0]))
            n_candles = len(candles)

    # Trades
    n_trades = 0
    if not os.path.exists(trade_path):
        trades = get(f"{BASE}/v1/trades/{symbol}?limit_trades=500") or []
        if trades:
            with open(trade_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp_ms", "tid", "price", "amount", "type"])
                w.writeheader()
                for t in sorted(trades, key=lambda x: x.get("timestampms", 0)):
                    w.writerow({
                        "timestamp_ms": t.get("timestampms"),
                        "tid":          t.get("tid"),
                        "price":        t.get("price"),
                        "amount":       t.get("amount"),
                        "type":         t.get("type"),
                    })
            n_trades = len(trades)

    return symbol, n_candles, n_trades


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    print("=== Gemini BTC Prediction Market Data Collector ===\n")

    # 1. Collect all events
    print("Step 1: Fetching all BTC events...")
    all_events = fetch_all_btc_events()
    print(f"  Total events: {len(all_events)}\n")

    # 2. Extract all contracts
    contracts = []
    for event in all_events:
        for c in event.get("contracts", []):
            prices = c.get("prices", {})
            contracts.append({
                "event_ticker":     event.get("ticker"),
                "event_title":      event.get("title"),
                "event_status":     event.get("status"),
                "event_created":    event.get("createdAt"),
                "event_resolved":   event.get("resolvedAt"),
                "contract_id":      c.get("id"),
                "contract_label":   c.get("label"),
                "instrument":       c.get("instrumentSymbol"),
                "contract_status":  c.get("status"),
                "expiry":           c.get("expiryDate"),
                "last_trade_price": prices.get("lastTradePrice"),
                "best_bid":         prices.get("bestBid"),
                "best_ask":         prices.get("bestAsk"),
                "total_shares":     c.get("totalShares"),
            })

    # Write events CSV (always refresh)
    events_path = os.path.join(OUT_DIR, "events.csv")
    with open(events_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(contracts[0].keys()))
        writer.writeheader()
        writer.writerows(contracts)
    print(f"  Wrote {len(contracts)} contracts -> {events_path}\n")

    # 3. Parallel market data collection
    symbols = [c["instrument"] for c in contracts if c.get("instrument")]
    print(f"Step 2: Collecting market data for {len(symbols)} instruments ({WORKERS} workers)...\n")

    lock = Lock()
    done = [0]
    total_candles = [0]
    total_trades  = [0]

    def worker(symbol):
        sym, nc, nt = collect_instrument(symbol)
        with lock:
            done[0] += 1
            total_candles[0] += nc
            total_trades[0]  += nt
            if done[0] % 200 == 0 or done[0] == len(symbols):
                pct = 100 * done[0] / len(symbols)
                print(
                    f"  {done[0]}/{len(symbols)} ({pct:.0f}%) | "
                    f"candles: {total_candles[0]} | trades: {total_trades[0]}"
                )
        return sym, nc, nt

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(worker, s) for s in symbols]
        for _ in as_completed(futures):
            pass

    # 4. Build combined files from per-instrument CSVs
    print("\nStep 3: Building combined files...")

    combined_candles_path = os.path.join(OUT_DIR, "combined_candles.csv")
    combined_trades_path  = os.path.join(OUT_DIR, "combined_trades.csv")

    with open(combined_candles_path, "w", newline="") as cf, \
         open(combined_trades_path,  "w", newline="") as tf:

        cw = csv.writer(cf)
        cw.writerow(["instrument", "timestamp_ms", "open", "high", "low", "close", "volume"])

        tw = csv.writer(tf)
        tw.writerow(["instrument", "timestamp_ms", "tid", "price", "amount", "type"])

        for sym in symbols:
            cp = os.path.join(CANDLES_DIR, f"{sym}.csv")
            tp = os.path.join(TRADES_DIR,  f"{sym}.csv")
            if os.path.exists(cp):
                with open(cp) as f:
                    reader = csv.reader(f)
                    next(reader)  # skip header
                    for row in reader:
                        cw.writerow([sym] + row)
            if os.path.exists(tp):
                with open(tp) as f:
                    reader = csv.reader(f)
                    next(reader)  # skip header
                    for row in reader:
                        tw.writerow([sym] + row)

    print(f"  -> {combined_candles_path}")
    print(f"  -> {combined_trades_path}")

    # Summary
    n_candle_files = len([f for f in os.listdir(CANDLES_DIR) if f.endswith(".csv")])
    n_trade_files  = len([f for f in os.listdir(TRADES_DIR)  if f.endswith(".csv")])
    print(f"\n=== Done ===")
    print(f"  Contracts:              {len(contracts)}")
    print(f"  Instruments w/ candles: {n_candle_files}")
    print(f"  Instruments w/ trades:  {n_trade_files}")
    print(f"  Output dir:             {OUT_DIR}/")


if __name__ == "__main__":
    main()
