#!/usr/bin/env python3
"""
Summarize open paper positions and unrealized PnL from the saved positions file.

Unrealized PnL uses the best currently executable exit price:
- buy positions mark to the current bid
- sell positions mark to the current ask
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.request
from pathlib import Path

BASE = "https://api.gemini.com"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "paper.ec2.json"
DEFAULT_POSITIONS = PROJECT_ROOT / "data" / "strategy" / "positions.ec2.json"
DEFAULT_FEE = 0.02


def load_json(path: Path):
    with path.open() as handle:
        return json.load(handle)


def get_json(url: str, timeout: int = 10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def fetch_order_book(symbol: str) -> tuple[float | None, float | None]:
    data = get_json(f"{BASE}/v1/book/{symbol}?limit_bids=1&limit_asks=1")
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    bid = float(bids[0]["price"]) if bids else None
    ask = float(asks[0]["price"]) if asks else None
    return bid, ask


def resolve_paths(args: argparse.Namespace) -> tuple[Path, float]:
    if args.positions:
        return Path(args.positions).expanduser().resolve(), DEFAULT_FEE

    if args.config:
        cfg_path = Path(args.config).expanduser().resolve()
    else:
        cfg_path = DEFAULT_CONFIG

    if cfg_path.exists():
        cfg = load_json(cfg_path)
        positions_rel = cfg.get("positions_path", str(DEFAULT_POSITIONS.relative_to(PROJECT_ROOT)))
        fee = float(cfg.get("paper_fee_per_contract", DEFAULT_FEE))
        return (PROJECT_ROOT / positions_rel).resolve(), fee

    return DEFAULT_POSITIONS, DEFAULT_FEE


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


def fmt_price(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


def fmt_money(value: float | None) -> str:
    return f"{value:+.2f}" if value is not None else "n/a"


def shorten(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize open paper positions and unrealized PnL")
    parser.add_argument("--config", help="Optional config JSON path")
    parser.add_argument("--positions", help="Optional positions JSON path override")
    args = parser.parse_args()

    positions_path, fee_per_contract = resolve_paths(args)
    if not positions_path.exists():
        raise SystemExit(f"Positions file not found: {positions_path}")

    raw_positions = load_json(positions_path)
    open_positions = [p for p in raw_positions if p.get("status") == "open"]

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"Open paper positions as of {now}")
    print(f"Positions file: {positions_path}")

    if not open_positions:
        print("No open positions.")
        return 0

    headers = ("instrument", "side", "qty", "entry", "bid", "ask", "mark", "gross", "net")
    rows: list[tuple[str, ...]] = []
    total_gross = 0.0
    total_net = 0.0
    marked_positions = 0

    for position in open_positions:
        symbol = position["instrument"]
        side = position["side"]
        qty = int(position["quantity"])
        entry_price = float(position["entry_price"])

        try:
            bid, ask = fetch_order_book(symbol)
            mark = mark_price(side, bid, ask)
        except Exception as exc:
            err = shorten(str(exc), 12)
            rows.append((shorten(symbol, 26), side, str(qty), fmt_price(entry_price), "err", "err", "err", err, err))
            continue

        gross = None
        net = None
        if mark is not None:
            gross = unrealized_pnl(side, qty, entry_price, mark)
            net = gross - (qty * fee_per_contract)
            total_gross += gross
            total_net += net
            marked_positions += 1

        rows.append(
            (
                shorten(symbol, 26),
                side,
                str(qty),
                fmt_price(entry_price),
                fmt_price(bid),
                fmt_price(ask),
                fmt_price(mark),
                fmt_money(gross),
                fmt_money(net),
            )
        )

    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]

    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))

    print("")
    print(f"Open positions: {len(open_positions)}")
    print(f"Marked positions: {marked_positions}")
    print(f"Total unrealized gross PnL: {total_gross:+.2f}")
    print(f"Total unrealized net PnL:   {total_net:+.2f}")
    print("Net PnL subtracts one paper fee per open contract for consistency with the trade blotter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
