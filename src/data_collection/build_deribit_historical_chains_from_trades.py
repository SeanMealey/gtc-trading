"""
Build daily Deribit BTC option-chain snapshots from a historical trade tape.

The output is a chain-like daily snapshot that is suitable for historical
calibration. It is not a true archived order-book snapshot, so the
"open_interest" field is populated with a trade-based liquidity proxy:
contracts traded in a configurable trailing lookback window.
"""

from __future__ import annotations

import argparse
import os

import pandas as pd


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
DEFAULT_TRADES_PATH = os.path.join(
    ROOT, "data", "deribit", "deribit_btc_options_feb_mar_2026.csv"
)
DEFAULT_OUTPUT_DIR = os.path.join(ROOT, "data", "deribit", "historical_chains")


OUTPUT_COLUMNS = [
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
    "last_trade_ts_ms",
    "last_trade_ts",
    "last_trade_price_btc",
    "trade_direction",
    "contracts_traded_lookback",
    "trade_amount_btc_lookback",
    "trades_in_lookback",
]


def progress_bar(index: int, total: int, label: str, width: int = 28) -> str:
    total = max(total, 1)
    filled = int(width * index / total)
    return f"[{'#' * filled}{'-' * (width - filled)}] {index}/{total} {label}"


def load_trade_tape(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["mark_price"] = pd.to_numeric(df["mark_price"], errors="coerce")
    df["iv"] = pd.to_numeric(df["iv"], errors="coerce")
    df["index_price"] = pd.to_numeric(df["index_price"], errors="coerce")
    df["contracts"] = pd.to_numeric(df["contracts"], errors="coerce").fillna(0.0)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    df["option_type"] = df["type"].map({"C": "call", "P": "put"})
    df["expiry_dt"] = pd.to_datetime(df["expiry"], format="%d%b%y", utc=True)
    df["date_utc"] = df["timestamp"].dt.date
    df.sort_values("timestamp", inplace=True)
    return df


def snapshot_datetimes(df: pd.DataFrame) -> list[pd.Timestamp]:
    return [
        pd.Timestamp(day).tz_localize("UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
        for day in sorted(df["date_utc"].unique())
    ]


def build_snapshot(df: pd.DataFrame, snapshot_dt: pd.Timestamp, lookback_hours: int) -> pd.DataFrame:
    lookback_start = snapshot_dt - pd.Timedelta(hours=lookback_hours)

    active = df[(df["timestamp"] <= snapshot_dt) & (df["expiry_dt"] > snapshot_dt)].copy()
    if active.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    latest = active.groupby("instrument_name", sort=False).tail(1).copy()

    lookback = active[active["timestamp"] > lookback_start].copy()
    liquidity = (
        lookback.groupby("instrument_name", sort=False)
        .agg(
            contracts_traded_lookback=("contracts", "sum"),
            trade_amount_btc_lookback=("amount", "sum"),
            trades_in_lookback=("instrument_name", "size"),
            volume_usd_24h=("index_price", lambda s: 0.0),
        )
        .reset_index()
    )

    # Compute USD notional using row-wise trade amount * index price.
    if not lookback.empty:
        usd_notional = (
            lookback.assign(volume_usd_trade=lookback["amount"] * lookback["index_price"])
            .groupby("instrument_name", sort=False)["volume_usd_trade"]
            .sum()
            .rename("volume_usd_24h")
            .reset_index()
        )
        liquidity = liquidity.drop(columns=["volume_usd_24h"]).merge(
            usd_notional,
            on="instrument_name",
            how="left",
        )

    snapshot = latest.merge(liquidity, on="instrument_name", how="left")
    snapshot["contracts_traded_lookback"] = snapshot["contracts_traded_lookback"].fillna(0.0)
    snapshot["trade_amount_btc_lookback"] = snapshot["trade_amount_btc_lookback"].fillna(0.0)
    snapshot["trades_in_lookback"] = snapshot["trades_in_lookback"].fillna(0).astype(int)
    snapshot["volume_usd_24h"] = snapshot["volume_usd_24h"].fillna(0.0)

    snapshot["snapshot_ts"] = snapshot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot["snapshot_ts_ms"] = int(snapshot_dt.timestamp() * 1000)
    snapshot["last_trade_ts_ms"] = (snapshot["timestamp"].astype("int64") // 10**6).astype("int64")
    snapshot["last_trade_ts"] = snapshot["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot["last_trade_price_btc"] = snapshot["price"]
    snapshot["trade_direction"] = snapshot["direction"]
    snapshot["underlying_price"] = snapshot["index_price"]
    snapshot["mark_iv"] = snapshot["iv"]
    snapshot["mark_price_btc"] = snapshot["mark_price"]
    snapshot["bid_price_btc"] = pd.NA
    snapshot["ask_price_btc"] = pd.NA
    snapshot["mid_price_btc"] = pd.NA
    snapshot["open_interest"] = snapshot["contracts_traded_lookback"]
    snapshot["expiry_ts_ms"] = (snapshot["expiry_dt"].astype("int64") // 10**6).astype("int64")
    snapshot["expiry_dt"] = snapshot["expiry_dt"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot["tte_years"] = (
        (pd.to_datetime(snapshot["expiry_dt"], utc=True) - snapshot_dt)
        .dt.total_seconds()
        .clip(lower=0.0)
        / (365.25 * 24 * 3600)
    )

    out = snapshot[OUTPUT_COLUMNS].copy()
    out.sort_values(["expiry_ts_ms", "strike", "option_type"], inplace=True)
    return out


def write_snapshot(snapshot: pd.DataFrame, output_dir: str) -> str:
    stamp = pd.to_datetime(snapshot["snapshot_ts"].iloc[0], utc=True).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"btc_options_chain_{stamp}.csv")
    os.makedirs(output_dir, exist_ok=True)
    snapshot.to_csv(path, index=False)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily historical Deribit option chains from a trade tape")
    parser.add_argument("--trades", default=DEFAULT_TRADES_PATH, help="Historical Deribit BTC option trade CSV")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for daily chain snapshots")
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Trailing liquidity window used to build open_interest proxy",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing daily snapshot files")
    args = parser.parse_args()

    df = load_trade_tape(args.trades)
    snapshots = snapshot_datetimes(df)
    print(f"Loaded {len(df)} trades across {len(snapshots)} UTC day(s).")

    written = 0
    skipped = 0
    for idx, snapshot_dt in enumerate(snapshots, start=1):
        label = snapshot_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        print(progress_bar(idx, len(snapshots), label))
        stamp = snapshot_dt.strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(args.output_dir, f"btc_options_chain_{stamp}.csv")
        if os.path.exists(output_path) and not args.overwrite:
            skipped += 1
            print(f"  skip: {output_path}")
            continue

        snapshot = build_snapshot(df, snapshot_dt, lookback_hours=args.lookback_hours)
        if snapshot.empty:
            print("  warning: no active instruments for snapshot")
            continue

        path = write_snapshot(snapshot, args.output_dir)
        written += 1
        print(
            f"  rows: {len(snapshot)} | calls: {(snapshot['option_type'] == 'call').sum()} | "
            f"positive-liquidity: {(snapshot['open_interest'] > 0).sum()}"
        )
        print(f"  saved: {path}")

    print("\nDone.")
    print(f"  snapshots: {len(snapshots)}")
    print(f"  written:   {written}")
    print(f"  skipped:   {skipped}")


if __name__ == "__main__":
    main()
