"""
Best-effort historical Deribit BTC options-chain reconstruction.

Important limitation
--------------------
Deribit's public API does not expose full historical options-chain snapshots
with historical bid/ask/open-interest for arbitrary past timestamps. This
script reconstructs a daily "chain-like" dataset from two official endpoints:

  1. public/get_instruments
     - instrument metadata, including creation/expiration timestamps
  2. public/get_last_trades_by_currency_and_time
     - historical option trades with mark_price, iv, and index_price

For each daily snapshot time, the script:
  - finds all BTC option instruments active at that timestamp
  - fetches BTC option trades in a configurable lookback window before it
  - keeps the latest trade per instrument
  - writes a chain-like CSV with metadata plus trade-derived market fields

This is suitable for reducing look-ahead bias in historical calibration, but it
is not a perfect substitute for true archived order-book snapshots.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://www.deribit.com/api/v2/public"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
DEFAULT_BACKTEST_CANDLES = os.path.join(
    ROOT, "data", "gemini_prediction_markets", "combined_candles.csv"
)
DEFAULT_OUTPUT_DIR = os.path.join(ROOT, "data", "deribit", "historical_chains")

FIELDNAMES = [
    "instrument_name",
    "option_type",
    "strike",
    "expiry_ts_ms",
    "expiry_dt",
    "tte_years",
    "underlying_price",
    "mark_iv",
    "mark_price_btc",
    "bid_price_btc",
    "ask_price_btc",
    "mid_price_btc",
    "open_interest",
    "volume_usd_24h",
    "snapshot_ts",
    "snapshot_ts_ms",
    "snapshot_source",
    "last_trade_ts_ms",
    "last_trade_ts",
    "last_trade_price_btc",
    "trade_direction",
    "trade_amount_btc",
    "trades_in_lookback",
    "contracts_traded_lookback",
    "trade_amount_btc_lookback",
    "creation_ts_ms",
    "instrument_state",
]


def progress_bar(index: int, total: int, label: str, width: int = 30) -> str:
    total = max(total, 1)
    filled = int(width * index / total)
    return f"[{'#' * filled}{'-' * (width - filled)}] {index}/{total} {label}"


def fetch_json(endpoint: str, params: dict[str, object], retries: int = 5) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}/{endpoint}?{query}"
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read())
            if "result" not in data:
                raise RuntimeError(f"Deribit response missing result: {data}")
            return data["result"]
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as exc:  # pragma: no cover - network behavior
            last_error = exc
            time.sleep(1 + attempt)

    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def utc_datetime_from_ms(timestamp_ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc)


def iso_utc(timestamp_ms: int) -> str:
    return utc_datetime_from_ms(timestamp_ms).strftime("%Y-%m-%dT%H:%M:%SZ")


def infer_period_from_backtest(candles_path: str) -> tuple[dt.date, dt.date]:
    min_ts = None
    max_ts = None
    with open(candles_path, newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts = int(row["timestamp_ms"])
            min_ts = ts if min_ts is None else min(min_ts, ts)
            max_ts = ts if max_ts is None else max(max_ts, ts)

    if min_ts is None or max_ts is None:
        raise RuntimeError(f"No timestamps found in {candles_path}")

    return utc_datetime_from_ms(min_ts).date(), utc_datetime_from_ms(max_ts).date()


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def date_range(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    days = (end_date - start_date).days
    return [start_date + dt.timedelta(days=offset) for offset in range(days + 1)]


def discover_instruments() -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()

    for expired in (False, True):
        payload = fetch_json(
            "get_instruments",
            {"currency": "BTC", "kind": "option", "expired": str(expired).lower()},
        )
        # Deribit documents a 1 req/sec sustained rate for get_instruments.
        time.sleep(1.05)

        for instrument in payload:
            name = instrument.get("instrument_name")
            if not name or name in seen:
                continue
            seen.add(name)
            result.append(instrument)

    result.sort(key=lambda item: item["creation_timestamp"])
    return result


def fetch_trades_window(start_ms: int, end_ms: int) -> list[dict]:
    result = fetch_json(
        "get_last_trades_by_currency_and_time",
        {
            "currency": "BTC",
            "kind": "option",
            "start_timestamp": start_ms,
            "end_timestamp": end_ms,
            "count": 1000,
            "sorting": "asc",
        },
    )
    return result.get("trades", [])


def aggregate_trades_for_snapshot(
    snapshot_ms: int, lookback_hours: int, chunk_minutes: int
) -> tuple[dict[str, dict], int]:
    lookback_ms = lookback_hours * 3600 * 1000
    start_ms = snapshot_ms - lookback_ms
    latest_by_instrument: dict[str, dict] = {}
    truncated_windows = 0

    chunk_ms = chunk_minutes * 60 * 1000
    total_windows = max(1, (lookback_ms + chunk_ms - 1) // chunk_ms)
    for idx in range(total_windows):
        window_start = start_ms + idx * chunk_ms
        window_end = min(snapshot_ms, window_start + chunk_ms - 1)
        trades = fetch_trades_window(window_start, window_end)
        if len(trades) >= 1000:
            truncated_windows += 1

        for trade in trades:
            name = trade["instrument_name"]
            info = latest_by_instrument.get(name)
            if info is None:
                info = {
                    "latest_trade": trade,
                    "trade_count": 0,
                    "contracts_sum": 0.0,
                    "amount_sum": 0.0,
                    "usd_notional_sum": 0.0,
                }
                latest_by_instrument[name] = info

            info["trade_count"] += 1
            info["contracts_sum"] += float(trade.get("contracts") or 0.0)
            info["amount_sum"] += float(trade.get("amount") or 0.0)
            info["usd_notional_sum"] += float(trade.get("amount") or 0.0) * float(
                trade.get("index_price") or 0.0
            )

            latest_trade = info["latest_trade"]
            if int(trade["timestamp"]) >= int(latest_trade["timestamp"]):
                info["latest_trade"] = trade

    return latest_by_instrument, truncated_windows


def active_instruments_at(
    instruments: list[dict],
    snapshot_ms: int,
) -> list[dict]:
    active = []
    for instrument in instruments:
        created = int(instrument["creation_timestamp"])
        expiry = int(instrument["expiration_timestamp"])
        if created <= snapshot_ms < expiry:
            active.append(instrument)
    return active


def build_rows_for_snapshot(
    instruments: list[dict],
    snapshot_ms: int,
    lookback_hours: int,
    chunk_minutes: int,
) -> tuple[list[dict], int]:
    active = active_instruments_at(instruments, snapshot_ms)
    trade_map, truncated_windows = aggregate_trades_for_snapshot(
        snapshot_ms,
        lookback_hours,
        chunk_minutes,
    )
    snapshot_dt = utc_datetime_from_ms(snapshot_ms)
    snapshot_iso = snapshot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = []
    for instrument in active:
        name = instrument["instrument_name"]
        expiry_ms = int(instrument["expiration_timestamp"])
        trade_info = trade_map.get(name)
        latest_trade = trade_info["latest_trade"] if trade_info else None
        expiry_dt = utc_datetime_from_ms(expiry_ms).strftime("%Y-%m-%dT%H:%M:%SZ")
        tte_years = max(0.0, (expiry_ms - snapshot_ms) / 1000 / 86400 / 365.25)

        row = {
            "instrument_name": name,
            "option_type": instrument.get("option_type"),
            "strike": instrument.get("strike"),
            "expiry_ts_ms": expiry_ms,
            "expiry_dt": expiry_dt,
            "tte_years": round(tte_years, 8),
            "underlying_price": latest_trade.get("index_price") if latest_trade else "",
            "mark_iv": latest_trade.get("iv") if latest_trade else "",
            "mark_price_btc": latest_trade.get("mark_price") if latest_trade else "",
            "bid_price_btc": "",
            "ask_price_btc": "",
            "mid_price_btc": "",
            # Deribit does not expose historical OI snapshots via this route.
            # Use contracts traded in the lookback window as a liquidity proxy.
            "open_interest": round(float(trade_info["contracts_sum"]), 8) if trade_info else 0.0,
            "volume_usd_24h": round(float(trade_info["usd_notional_sum"]), 8) if trade_info else 0.0,
            "snapshot_ts": snapshot_iso,
            "snapshot_ts_ms": snapshot_ms,
            "snapshot_source": "trade_derived_latest_trade_in_lookback" if latest_trade else "metadata_only",
            "last_trade_ts_ms": latest_trade.get("timestamp") if latest_trade else "",
            "last_trade_ts": iso_utc(int(latest_trade["timestamp"])) if latest_trade else "",
            "last_trade_price_btc": latest_trade.get("price") if latest_trade else "",
            "trade_direction": latest_trade.get("direction") if latest_trade else "",
            "trade_amount_btc": latest_trade.get("amount") if latest_trade else "",
            "trades_in_lookback": trade_info["trade_count"] if trade_info else 0,
            "contracts_traded_lookback": round(float(trade_info["contracts_sum"]), 8) if trade_info else 0.0,
            "trade_amount_btc_lookback": round(float(trade_info["amount_sum"]), 8) if trade_info else 0.0,
            "creation_ts_ms": instrument.get("creation_timestamp"),
            "instrument_state": instrument.get("state"),
        }
        rows.append(row)

    rows.sort(key=lambda item: (item["expiry_ts_ms"], item["strike"], item["option_type"] or ""))
    return rows, truncated_windows


def write_snapshot(rows: list[dict], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct historical BTC option chain snapshots from Deribit public data")
    parser.add_argument("--start-date", help="UTC start date YYYY-MM-DD")
    parser.add_argument("--end-date", help="UTC end date YYYY-MM-DD")
    parser.add_argument(
        "--backtest-candles",
        default=DEFAULT_BACKTEST_CANDLES,
        help="Use this file to infer the historical backtest period if start/end are omitted",
    )
    parser.add_argument(
        "--snapshot-hour",
        type=int,
        default=0,
        help="UTC snapshot hour for each day (default: 0)",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Trade lookback window before each snapshot (default: 24)",
    )
    parser.add_argument(
        "--chunk-minutes",
        type=int,
        default=60,
        help="Trade-fetch chunk size in minutes (default: 60)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write timestamped chain CSVs",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing snapshot CSV",
    )
    args = parser.parse_args()

    if args.start_date and args.end_date:
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
    else:
        start_date, end_date = infer_period_from_backtest(args.backtest_candles)

    if end_date < start_date:
        raise SystemExit("end-date must be on or after start-date")
    if not (0 <= args.snapshot_hour <= 23):
        raise SystemExit("snapshot-hour must be between 0 and 23")
    if args.lookback_hours < 1:
        raise SystemExit("lookback-hours must be >= 1")
    if args.chunk_minutes < 1:
        raise SystemExit("chunk-minutes must be >= 1")

    snapshot_days = date_range(start_date, end_date)
    snapshot_times = [
        dt.datetime.combine(day, dt.time(args.snapshot_hour, 0), tzinfo=dt.timezone.utc)
        for day in snapshot_days
    ]

    print("=== Deribit Historical BTC Options Collector ===")
    print(f"Period: {start_date} -> {end_date} UTC")
    print(f"Snapshot hour: {args.snapshot_hour:02d}:00 UTC")
    print(f"Lookback: {args.lookback_hours} hours")
    print(f"Output dir: {args.output_dir}")

    print("\nFetching BTC option instrument metadata...")
    instruments = discover_instruments()
    print(f"  instruments discovered: {len(instruments)}")

    total = len(snapshot_times)
    written = 0
    skipped = 0
    for idx, snapshot_dt in enumerate(snapshot_times, start=1):
        label = snapshot_dt.strftime("%Y-%m-%d %H:%M UTC")
        print(f"\n{progress_bar(idx, total, label)}")
        stamp = snapshot_dt.strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(args.output_dir, f"btc_options_chain_{stamp}.csv")
        if os.path.exists(output_path) and not args.overwrite:
            skipped += 1
            print(f"  skip: {output_path}")
            continue

        snapshot_ms = int(snapshot_dt.timestamp() * 1000)
        active = active_instruments_at(instruments, snapshot_ms)
        print(f"  active instruments at snapshot: {len(active)}")

        rows, truncated_windows = build_rows_for_snapshot(
            instruments=instruments,
            snapshot_ms=snapshot_ms,
            lookback_hours=args.lookback_hours,
            chunk_minutes=args.chunk_minutes,
        )
        traded = sum(1 for row in rows if row["snapshot_source"] != "metadata_only")
        print(f"  rows written: {len(rows)} | with trade data: {traded}")
        if truncated_windows:
            print(f"  warning: {truncated_windows} chunk(s) hit the 1000-trade API cap")
        write_snapshot(rows, output_path)
        written += 1
        print(f"  saved: {output_path}")

    print("\nDone.")
    print(f"  snapshots attempted: {total}")
    print(f"  snapshots written:   {written}")
    print(f"  snapshots skipped:   {skipped}")


if __name__ == "__main__":
    main()
