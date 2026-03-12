"""
Fetch settlement outcomes for all settled Gemini BTC prediction market events.

For each settled event, calls GET /v1/prediction-markets/events/{eventTicker}
and extracts resolutionSide (yes/no -> 1/0) per contract.

Output: data/gemini_prediction_markets/settlements.csv
Columns: instrument, event_ticker, expiry, resolved_at, strike, outcome, resolution_side

- outcome=1  -> contract paid $1 (BTC > strike at expiry)
- outcome=0  -> contract paid $0
- outcome='' -> non-standard ticker format (old categorical markets), raw resolution_side kept
"""

import urllib.request
import urllib.error
import json
import csv
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

BASE    = "https://api.gemini.com"
OUT_DIR = "data/gemini_prediction_markets"
OUT_PATH = os.path.join(OUT_DIR, "settlements.csv")
WORKERS = 10

FIELDNAMES = ["instrument", "event_ticker", "expiry", "resolved_at",
              "strike", "outcome", "resolution_side"]


# ── helpers ───────────────────────────────────────────────────────────────────

def get(url, retries=4):
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


def parse_strike(instrument_symbol):
    """
    Extract numeric strike from instrument symbol.
    GEMI-BTC2602040800-HI82500  ->  82500
    GEMI-BTC2603030000-HI69000  ->  69000
    Returns None for non-standard formats (e.g. old LO80K / 80KTO90K events).
    """
    m = re.search(r"-HI(\d+)$", instrument_symbol)
    if m:
        return int(m.group(1))
    return None


def fetch_event_settlements(event_ticker):
    """
    Fetch one event and return a list of settlement row dicts.
    """
    data = get(f"{BASE}/v1/prediction-markets/events/{event_ticker}")
    if not data:
        return []

    rows = []
    resolved_at = data.get("resolvedAt", "")
    for c in data.get("contracts", []):
        if c.get("status") != "settled":
            continue
        instrument = c.get("instrumentSymbol", "")
        resolution_side = c.get("resolutionSide", "")  # "yes" or "no"
        expiry = c.get("expiryDate", "")
        contract_resolved_at = c.get("resolvedAt", resolved_at)

        strike = parse_strike(instrument)

        if resolution_side == "yes":
            outcome = 1
        elif resolution_side == "no":
            outcome = 0
        else:
            outcome = ""

        rows.append({
            "instrument":      instrument,
            "event_ticker":    event_ticker,
            "expiry":          expiry,
            "resolved_at":     contract_resolved_at,
            "strike":          strike if strike is not None else "",
            "outcome":         outcome,
            "resolution_side": resolution_side,
        })
    return rows


def load_done_tickers(path):
    """Return set of event_tickers already written to the output file."""
    done = set()
    if not os.path.exists(path):
        return done
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            done.add(row["event_ticker"])
    return done


def load_settled_event_tickers(events_path):
    """Read unique settled event tickers from events.csv."""
    tickers = set()
    with open(events_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["event_status"] == "settled":
                tickers.add(row["event_ticker"])
    return sorted(tickers)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    events_path = os.path.join(OUT_DIR, "events.csv")
    if not os.path.exists(events_path):
        print(f"ERROR: {events_path} not found. Run get_gemini_prediction_markets.py first.")
        return

    all_tickers = load_settled_event_tickers(events_path)
    print(f"Settled event tickers: {len(all_tickers)}")

    done_tickers = load_done_tickers(OUT_PATH)
    remaining = [t for t in all_tickers if t not in done_tickers]
    print(f"Already fetched: {len(done_tickers)} | Remaining: {len(remaining)}")

    if not remaining:
        print("All settlements already fetched.")
        return

    # Append mode — write header only if file is new
    write_header = not os.path.exists(OUT_PATH)
    outfile = open(OUT_PATH, "a", newline="")
    writer = csv.DictWriter(outfile, fieldnames=FIELDNAMES)
    if write_header:
        writer.writeheader()

    lock = Lock()
    done_count = [len(done_tickers)]
    total = len(all_tickers)

    def worker(ticker):
        rows = fetch_event_settlements(ticker)
        with lock:
            if rows:
                writer.writerows(rows)
                outfile.flush()
            done_count[0] += 1
            if done_count[0] % 25 == 0 or done_count[0] == total:
                print(f"  {done_count[0]}/{total} events | {ticker}")
        return ticker, len(rows)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(worker, t) for t in remaining]
        for _ in as_completed(futures):
            pass

    outfile.close()

    # Summary
    with open(OUT_PATH, newline="") as f:
        n_rows = sum(1 for _ in csv.DictReader(f))
    print(f"\nDone. {n_rows} settled contracts written to {OUT_PATH}")

    # Quick outcome breakdown
    yes_count = no_count = blank_count = 0
    with open(OUT_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if row["outcome"] == "1":
                yes_count += 1
            elif row["outcome"] == "0":
                no_count += 1
            else:
                blank_count += 1
    print(f"  outcome=1 (paid): {yes_count}")
    print(f"  outcome=0 (lapsed): {no_count}")
    if blank_count:
        print(f"  outcome='' (non-HI format): {blank_count}")


if __name__ == "__main__":
    main()
