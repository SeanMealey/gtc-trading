#!/usr/bin/env python3
"""
Unified CLI report for paper-trading results.

This combines the old settled-trades report and open-position MTM summary into
one account-level view so the key numbers reconcile in one place.

Examples:
    python3 paper_results_report.py
    python3 paper_results_report.py --skip-market
    python3 paper_results_report.py data/strategy/paper_trades.ec2.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path


BASE = "https://api.gemini.com"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "paper.ec2.json"
DEFAULT_POSITIONS = PROJECT_ROOT / "data" / "strategy" / "positions.ec2.json"
DEFAULT_FEE = 0.02
DEFAULT_STARTING_CAPITAL = 3000.0
DEFAULT_TRADE_CANDIDATES = [
    PROJECT_ROOT / "data" / "strategy" / "paper_trades.ec2.v2.csv",
    PROJECT_ROOT / "data" / "strategy" / "paper_trades.ec2.csv",
    PROJECT_ROOT / "data" / "strategy" / "paper_trades.csv",
]


@dataclass
class OpenPosition:
    instrument: str
    side: str
    quantity: int
    entry_price: float


def load_json(path: Path):
    with path.open() as handle:
        return json.load(handle)


def get_json(url: str, timeout: int = 10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def fetch_order_book(symbol: str, timeout: int) -> tuple[float | None, float | None]:
    data = get_json(f"{BASE}/v1/book/{symbol}?limit_bids=1&limit_asks=1", timeout=timeout)
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    bid = float(bids[0]["price"]) if bids else None
    ask = float(asks[0]["price"]) if asks else None
    return bid, ask


def to_float(value: str | None) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def to_int(value: str | None) -> int:
    if value is None or value == "":
        return 0
    return int(float(value))


def fmt_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def fmt_signed_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}"


def fmt_price(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


def shorten(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def mark_price(side: str, bid: float | None, ask: float | None) -> float | None:
    if side == "buy":
        return bid
    if side == "sell":
        return ask
    raise ValueError(f"unsupported side: {side!r}")


def unrealized_pnl(side: str, qty: int, entry_price: float, mark: float) -> float:
    if side == "buy":
        return (mark - entry_price) * qty
    if side == "sell":
        return (entry_price - mark) * qty
    raise ValueError(f"unsupported side: {side!r}")


def max_loss_per_contract(entry_price: float, side: str) -> float:
    if side == "buy":
        return max(entry_price, 1e-9)
    if side == "sell":
        return max(1.0 - entry_price, 1e-9)
    raise ValueError(f"unsupported side: {side!r}")


def resolve_default_trades_path() -> Path:
    for candidate in DEFAULT_TRADE_CANDIDATES:
        if candidate.exists():
            return candidate
    return DEFAULT_TRADE_CANDIDATES[0]


def resolve_inputs(args: argparse.Namespace) -> tuple[Path, Path, float, float]:
    cfg_path = Path(args.config).expanduser().resolve() if args.config else DEFAULT_CONFIG
    cfg = {}
    if cfg_path.exists():
        cfg = load_json(cfg_path)

    trades_path: Path
    if args.trades:
        trades_path = Path(args.trades).expanduser()
        if not trades_path.is_absolute():
            trades_path = (PROJECT_ROOT / trades_path).resolve()
    elif cfg.get("paper_trades_path"):
        trades_path = (PROJECT_ROOT / cfg["paper_trades_path"]).resolve()
    else:
        trades_path = resolve_default_trades_path()

    positions_path: Path
    if args.positions:
        positions_path = Path(args.positions).expanduser()
        if not positions_path.is_absolute():
            positions_path = (PROJECT_ROOT / positions_path).resolve()
    elif cfg.get("positions_path"):
        positions_path = (PROJECT_ROOT / cfg["positions_path"]).resolve()
    else:
        positions_path = DEFAULT_POSITIONS

    fee_per_contract = float(cfg.get("paper_fee_per_contract", DEFAULT_FEE))
    starting_capital = float(cfg.get("total_capital_usd", DEFAULT_STARTING_CAPITAL))
    return trades_path, positions_path, fee_per_contract, starting_capital


def load_open_positions(positions_path: Path, open_rows: list[dict[str, str]]) -> tuple[list[OpenPosition], str | None]:
    if positions_path.exists():
        raw_positions = load_json(positions_path)
        positions = [
            OpenPosition(
                instrument=position["instrument"],
                side=position["side"],
                quantity=int(position["quantity"]),
                entry_price=float(position["entry_price"]),
            )
            for position in raw_positions
            if position.get("status") == "open"
        ]
        return positions, None

    fallback_positions = [
        OpenPosition(
            instrument=row["instrument"],
            side=row["side"],
            quantity=to_int(row.get("quantity")),
            entry_price=to_float(row.get("entry_price")),
        )
        for row in open_rows
    ]
    warning = f"Positions file not found, using open rows from trade blotter: {positions_path}"
    return fallback_positions, warning


def print_key_value(label: str, value: str) -> None:
    print(f"{label:<30} {value}")


def collect_open_positions_table(
    positions: list[OpenPosition],
    fee_per_contract: float,
    skip_market: bool,
    timeout: int,
) -> tuple[list[tuple[str, ...]], int, float, float]:
    if not positions:
        return [], 0, 0.0, 0.0

    rows: list[tuple[str, ...]] = []
    marked_positions = 0
    total_gross = 0.0
    total_net = 0.0

    for position in positions:
        if skip_market:
            rows.append(
                (
                    shorten(position.instrument, 26),
                    position.side,
                    str(position.quantity),
                    fmt_price(position.entry_price),
                    "skip",
                    "skip",
                    "skip",
                    "skip",
                    "skip",
                )
            )
            continue

        try:
            bid, ask = fetch_order_book(position.instrument, timeout=timeout)
            mark = mark_price(position.side, bid, ask)
        except Exception as exc:
            err = shorten(str(exc), 12)
            rows.append(
                (
                    shorten(position.instrument, 26),
                    position.side,
                    str(position.quantity),
                    fmt_price(position.entry_price),
                    "err",
                    "err",
                    "err",
                    err,
                    err,
                )
            )
            continue

        gross = None
        net = None
        if mark is not None:
            gross = unrealized_pnl(position.side, position.quantity, position.entry_price, mark)
            net = gross - (position.quantity * fee_per_contract)
            total_gross += gross
            total_net += net
            marked_positions += 1

        rows.append(
            (
                shorten(position.instrument, 26),
                position.side,
                str(position.quantity),
                fmt_price(position.entry_price),
                fmt_price(bid),
                fmt_price(ask),
                fmt_price(mark),
                fmt_signed_money(gross),
                fmt_signed_money(net),
            )
        )

    return rows, marked_positions, total_gross, total_net


def print_open_positions_table(rows: list[tuple[str, ...]]) -> None:
    print("")
    print("Open positions")

    if not rows:
        print("No open positions.")
        return

    headers = ("instrument", "side", "qty", "entry", "bid", "ask", "mark", "gross", "net")
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]

    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a unified paper-trading account summary")
    parser.add_argument(
        "trades",
        nargs="?",
        help="Optional path to the paper trades CSV",
    )
    parser.add_argument("--config", help="Optional config JSON path")
    parser.add_argument("--positions", help="Optional positions JSON path override")
    parser.add_argument(
        "--skip-market",
        action="store_true",
        help="Skip live order-book lookups and omit unrealized MTM metrics",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="HTTP timeout in seconds for market-data requests",
    )
    args = parser.parse_args()

    trades_path, positions_path, fee_per_contract, starting_capital = resolve_inputs(args)
    if not trades_path.exists():
        raise SystemExit(f"Trades file not found: {trades_path}")

    with trades_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    settled_rows = [row for row in rows if row.get("status") == "settled"]
    open_rows = [row for row in rows if row.get("status") == "open"]
    open_positions, position_warning = load_open_positions(positions_path, open_rows)

    total_trades = len(rows)
    realized_gross_pnl = sum(to_float(row.get("gross_total_pnl")) for row in settled_rows)
    realized_net_pnl = sum(to_float(row.get("net_total_pnl")) for row in settled_rows)
    realized_fees = sum(to_float(row.get("fees_total")) for row in settled_rows)
    estimated_open_fees = sum(position.quantity * fee_per_contract for position in open_positions)
    total_fees = realized_fees + estimated_open_fees
    wins = sum(1 for row in settled_rows if to_float(row.get("net_total_pnl")) > 0)
    losses = sum(1 for row in settled_rows if to_float(row.get("net_total_pnl")) < 0)
    buys = sum(1 for row in rows if row.get("side") == "buy")
    sells = sum(1 for row in rows if row.get("side") == "sell")
    total_qty = sum(to_int(row.get("quantity")) for row in rows)
    settled_qty = sum(to_int(row.get("quantity")) for row in settled_rows)
    open_qty = sum(position.quantity for position in open_positions)
    open_notional = sum(to_float(row.get("entry_notional_usd")) for row in open_rows)
    open_max_loss = sum(
        position.quantity * max_loss_per_contract(position.entry_price, position.side)
        for position in open_positions
    )
    avg_net = realized_net_pnl / len(settled_rows) if settled_rows else 0.0
    win_rate = wins / len(settled_rows) if settled_rows else 0.0
    available_to_risk = starting_capital + realized_net_pnl - open_max_loss

    open_position_rows, marked_positions, unrealized_gross_pnl, unrealized_net_pnl = collect_open_positions_table(
        positions=open_positions,
        fee_per_contract=fee_per_contract,
        skip_market=args.skip_market,
        timeout=args.timeout,
    )
    marked_equity = None
    if not args.skip_market and marked_positions == len(open_positions):
        marked_equity = starting_capital + realized_net_pnl + unrealized_net_pnl

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("")
    print(f"Paper trading summary as of {timestamp}")
    print_key_value("Trades file", str(trades_path))
    print_key_value("Positions file", str(positions_path))
    print_key_value("Starting capital", fmt_money(starting_capital))

    if position_warning:
        print_key_value("Warning", position_warning)
    elif len(open_rows) != len(open_positions):
        print_key_value(
            "Warning",
            (
                "Open trade rows and open positions differ "
                f"({len(open_rows)} vs {len(open_positions)}); using positions file for MTM"
            ),
        )

    print("")
    print("Account")
    print_key_value("Realized net PnL", fmt_money(realized_net_pnl))
    print_key_value("Unrealized net PnL", fmt_money(unrealized_net_pnl if not args.skip_market else None))
    print_key_value("Marked account equity", fmt_money(marked_equity))
    print_key_value("Open max loss reserved", fmt_money(open_max_loss))
    print_key_value("Available to risk", fmt_money(available_to_risk))

    print("")
    print("PnL and fees")
    print_key_value("Realized gross PnL", fmt_money(realized_gross_pnl))
    print_key_value("Realized net PnL", fmt_money(realized_net_pnl))
    print_key_value("Unrealized gross PnL", fmt_money(unrealized_gross_pnl if not args.skip_market else None))
    print_key_value("Unrealized net PnL", fmt_money(unrealized_net_pnl if not args.skip_market else None))
    print_key_value("Realized fees", fmt_money(realized_fees))
    print_key_value("Estimated open fees", fmt_money(estimated_open_fees))
    print_key_value("Total fees", fmt_money(total_fees))

    print("")
    print("Trades")
    print_key_value("Total trades", str(total_trades))
    print_key_value("Settled trades", str(len(settled_rows)))
    print_key_value("Open trades", str(len(open_positions)))
    print_key_value("Buy trades", str(buys))
    print_key_value("Sell trades", str(sells))
    print_key_value("Total quantity", str(total_qty))
    print_key_value("Settled quantity", str(settled_qty))
    print_key_value("Open quantity", str(open_qty))
    print_key_value("Avg net per settled", fmt_money(avg_net))
    print_key_value("Wins", str(wins))
    print_key_value("Losses", str(losses))
    print_key_value("Win rate", f"{win_rate:.1%}")

    print("")
    print("Open exposure")
    print_key_value("Open entry notional", fmt_money(open_notional))
    print_key_value("Open max loss", fmt_money(open_max_loss))
    marked_open_positions_text = (
        "skipped"
        if args.skip_market
        else f"{marked_positions}/{len(open_positions)}"
    )
    print_key_value("Marked open positions", marked_open_positions_text)

    print("")
    print("Notes")
    print("  marked_account_equity = starting_capital + realized_net_pnl + unrealized_net_pnl")
    print("  available_to_risk = starting_capital + realized_net_pnl - open_max_loss")
    print("  estimated_open_fees are reserved for consistency with the paper blotter and are not settled yet")
    if args.skip_market:
        print("  live MTM was skipped, so unrealized values and marked equity are omitted")
    elif marked_positions != len(open_positions):
        print("  some open positions could not be marked, so marked equity is omitted")

    print_open_positions_table(open_position_rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
