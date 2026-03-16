#!/usr/bin/env python3
"""
Simple CLI report for paper-trade results.

Examples:
    python3 paper_results_report.py
    python3 paper_results_report.py data/strategy/paper_trades.ec2.v2.csv
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CANDIDATES = [
    PROJECT_ROOT / "data" / "strategy" / "paper_trades.ec2.v2.csv",
    PROJECT_ROOT / "data" / "strategy" / "paper_trades.ec2.csv",
    PROJECT_ROOT / "data" / "strategy" / "paper_trades.csv",
]


def resolve_default_trades_path() -> Path:
    for candidate in DEFAULT_CANDIDATES:
        if candidate.exists():
            return candidate
    return DEFAULT_CANDIDATES[0]


def to_float(value: str | None) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def to_int(value: str | None) -> int:
    if value is None or value == "":
        return 0
    return int(float(value))


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a simple paper-trading results report")
    parser.add_argument(
        "trades",
        nargs="?",
        default=str(resolve_default_trades_path()),
        help="Path to the paper trades CSV",
    )
    args = parser.parse_args()

    trades_path = Path(args.trades).expanduser()
    if not trades_path.is_absolute():
        trades_path = (PROJECT_ROOT / trades_path).resolve()

    if not trades_path.exists():
        raise SystemExit(f"Trades file not found: {trades_path}")

    with trades_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    total_trades = len(rows)
    settled_rows = [row for row in rows if row.get("status") == "settled"]
    open_rows = [row for row in rows if row.get("status") == "open"]

    gross_pnl = sum(to_float(row.get("gross_total_pnl")) for row in settled_rows)
    net_pnl = sum(to_float(row.get("net_total_pnl")) for row in settled_rows)
    total_fees = sum(to_float(row.get("fees_total")) for row in rows)
    wins = sum(1 for row in settled_rows if to_float(row.get("net_total_pnl")) > 0)
    losses = sum(1 for row in settled_rows if to_float(row.get("net_total_pnl")) < 0)
    buys = sum(1 for row in rows if row.get("side") == "buy")
    sells = sum(1 for row in rows if row.get("side") == "sell")
    open_risk = sum(to_float(row.get("max_loss_usd")) for row in open_rows)
    open_notional = sum(to_float(row.get("entry_notional_usd")) for row in open_rows)
    total_qty = sum(to_int(row.get("quantity")) for row in rows)
    settled_qty = sum(to_int(row.get("quantity")) for row in settled_rows)
    avg_net = net_pnl / len(settled_rows) if settled_rows else 0.0
    win_rate = wins / len(settled_rows) if settled_rows else 0.0

    print(f"trades_file:           {trades_path}")
    print(f"total_trades:          {total_trades}")
    print(f"settled_trades:        {len(settled_rows)}")
    print(f"open_trades:           {len(open_rows)}")
    print(f"buy_trades:            {buys}")
    print(f"sell_trades:           {sells}")
    print(f"total_quantity:        {total_qty}")
    print(f"settled_quantity:      {settled_qty}")
    print(f"gross_pnl:             ${gross_pnl:,.2f}")
    print(f"net_pnl:               ${net_pnl:,.2f}")
    print(f"total_fees:            ${total_fees:,.2f}")
    print(f"avg_net_per_settled:   ${avg_net:,.2f}")
    print(f"wins:                  {wins}")
    print(f"losses:                {losses}")
    print(f"win_rate:              {win_rate:.1%}")
    print(f"open_entry_notional:   ${open_notional:,.2f}")
    print(f"open_max_loss:         ${open_risk:,.2f}")


if __name__ == "__main__":
    main()
