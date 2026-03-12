#!/usr/bin/env python3
"""
Live dashboard: Gemini BTC prediction market model prices vs market bid/ask.

Usage:
    python src/live_dashboard.py
    python src/live_dashboard.py --params data/deribit/bates_params_implied.json
    python src/live_dashboard.py --interval 10

Requires: pip install rich
"""

import sys
import os
import time
import datetime
import urllib.request
import json
import re
import argparse
import math

# ── path setup ────────────────────────────────────────────────────────────────
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SRC_DIR, "pricer"))
sys.path.insert(0, SRC_DIR)

import bates_pricer as bp
from calibration.params import BatesParams

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.columns import Columns
from rich import box

# ── constants ─────────────────────────────────────────────────────────────────
BASE = "https://api.gemini.com"
DEFAULT_PARAMS_PATH = os.path.join(SRC_DIR, "..", "data", "deribit", "bates_params_implied.json")
FALLBACK_PARAMS_PATH = os.path.join(SRC_DIR, "..", "data", "bates_params_implied.json")
RISK_FREE_RATE = 0.045  # fallback if not in params

# Fallback Bates params (reasonable BTC defaults, clearly labelled)
FALLBACK_PARAMS = BatesParams(
    S=0.0,       # filled in at runtime from spot
    r=RISK_FREE_RATE,
    q=0.0,
    v0=0.45 ** 2,
    kappa=2.0,
    theta=0.40 ** 2,
    sigma_v=0.80,
    rho=-0.55,
    lam=4.0,
    mu_j=-0.04,
    sigma_j=0.20,
    calibration_source="fallback-defaults",
    calibrated_at="n/a",
)


# ── API helpers ───────────────────────────────────────────────────────────────

def get_json(url: str, timeout: int = 8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_active_events():
    url = f"{BASE}/v1/prediction-markets/events?limit=100&search=BTC&status=active"
    data = get_json(url)
    if not data:
        return []
    return data.get("data", [])


def fetch_spot_price() -> float | None:
    data = get_json(f"{BASE}/v1/pubticker/BTCUSD")
    if not data:
        return None
    try:
        return float(data["last"])
    except (KeyError, ValueError):
        return None


# ── contract parsing ──────────────────────────────────────────────────────────

_TICKER_RE = re.compile(r"GEMI-BTC(\d{10})-HI(\d+)")


def parse_instrument(symbol: str):
    """
    Parse expiry datetime and strike from ticker symbol.
    Format: GEMI-BTC{YYMMDDHHII}-HI{PRICE}
    Returns (expiry_dt, strike) or (None, None) on failure.
    """
    m = _TICKER_RE.match(symbol)
    if not m:
        return None, None
    ts, strike_str = m.group(1), m.group(2)
    try:
        expiry_dt = datetime.datetime(
            int("20" + ts[0:2]),   # YY → YYYY
            int(ts[2:4]),          # MM
            int(ts[4:6]),          # DD
            int(ts[6:8]),          # HH
            int(ts[8:10]),         # II (minute)
            tzinfo=datetime.timezone.utc,
        )
        return expiry_dt, float(strike_str)
    except ValueError:
        return None, None


_INDEX_RE = re.compile(r"(GRR-KAIKO_BTCUSD_1S|KK_BRR_BTCUSD)")
_index_cache: dict[str, str] = {}  # event_ticker -> index string


def fetch_event_index(event_ticker: str) -> str:
    """
    Fetch the settlement index for an event from the Gemini detail endpoint.
    The index is embedded in each contract's description rich-text field.
    Result is cached — fetched once per event ticker per dashboard session.
    Falls back to GRR-KAIKO_BTCUSD_1S if unavailable.
    """
    if event_ticker in _index_cache:
        return _index_cache[event_ticker]

    data = get_json(f"{BASE}/v1/prediction-markets/events/{event_ticker}")
    idx = "GRR-KAIKO_BTCUSD_1S"  # safe fallback

    if data:
        for contract in data.get("contracts", []):
            desc = contract.get("description", {})
            text = ""
            for block in desc.get("content", []):
                for item in block.get("content", [block]):
                    text += item.get("value", "")
            m = _INDEX_RE.search(text)
            if m:
                idx = m.group(1)
                break

    _index_cache[event_ticker] = idx
    return idx


# ── pricing ───────────────────────────────────────────────────────────────────

def price_contract(strike: float, T: float, bates_params: BatesParams) -> float:
    """Price a binary call using the C++ COS pricer."""
    p = bates_params.to_pricer_params()
    return bp.binary_call(strike, T, p, N=256)


# ── display ───────────────────────────────────────────────────────────────────

def _fmt_price(val) -> str:
    return f"{val:.3f}" if val is not None else "—"


def _realizable_edge(model, bid, ask) -> tuple[float | None, str]:
    """
    Edge against the price you'd actually trade at:
      model > ask  → BUY at ask,  edge = model - ask  (positive = real edge)
      model < bid  → SELL at bid, edge = bid - model  (positive = real edge)
      otherwise    → inside spread, no realizable edge
    Returns (edge_value, side) where side is 'buy', 'sell', or 'none'.
    """
    if model is None:
        return None, "none"
    if ask is not None and model > ask:
        return model - ask, "buy"
    if bid is not None and model < bid:
        return bid - model, "sell"
    return None, "none"


def _edge_text(edge: float | None, side: str, bid: float | None, ask: float | None) -> Text:
    if edge is None:
        if bid is None and ask is None:
            return Text("no book", style="dim")
        if bid is None or ask is None:
            return Text("one-sided", style="dim")
        return Text("inside spread", style="dim")
    label = f"+{edge:.3f} {'BUY' if side == 'buy' else 'SELL'}"
    style = "bold green" if edge > 0.015 else "yellow"
    return Text(label, style=style)


MODEL_FILTER = 0.03   # hide contracts with model price outside [FILTER, 1-FILTER]


def build_event_table(event: dict, bates_params: BatesParams, spot: float, now: datetime.datetime) -> Table:
    ticker = event.get("ticker", "?")
    title_str = event.get("title", ticker)

    tbl = Table(
        title=f"[bold]{title_str}[/bold]  [dim]({ticker})[/dim]",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        title_justify="left",
        expand=True,
    )
    tbl.add_column("Instrument", style="dim", no_wrap=True)
    tbl.add_column("Strike", justify="right", style="white")
    tbl.add_column("Expiry (UTC)", justify="center")
    tbl.add_column("T left", justify="right")
    tbl.add_column("Index", justify="center", style="yellow")
    tbl.add_column("Bid", justify="right", style="red")
    tbl.add_column("Ask", justify="right", style="green")
    tbl.add_column("Mid", justify="right")
    tbl.add_column("Model", justify="right", style="bold white")
    tbl.add_column("Edge (Executable)", justify="right")

    contracts = event.get("contracts", [])
    # Sort by strike ascending
    parsed = []
    for c in contracts:
        sym = c.get("instrumentSymbol", "")
        expiry_dt, strike = parse_instrument(sym)
        if expiry_dt is None:
            continue
        T = (expiry_dt - now).total_seconds() / (365.25 * 24 * 3600)
        if T <= 0:
            continue  # already expired
        parsed.append((strike, sym, expiry_dt, T, c))

    parsed.sort(key=lambda x: x[0])

    # Pre-compute model prices and filter far OTM/ITM rows
    priced = []
    for strike, sym, expiry_dt, T, c in parsed:
        bates_params.S = spot
        try:
            model = price_contract(strike, T, bates_params)
        except Exception:
            model = None
        priced.append((strike, sym, expiry_dt, T, c, model))

    visible = [(s, sy, e, t, c, m) for s, sy, e, t, c, m in priced
               if m is None or MODEL_FILTER <= m <= 1 - MODEL_FILTER]
    n_hidden = len(priced) - len(visible)

    for strike, sym, expiry_dt, T, c, model in visible:
        prices = c.get("prices", {})
        bid_raw = prices.get("bestBid")
        ask_raw = prices.get("bestAsk")
        bid = float(bid_raw) if bid_raw else None
        ask = float(ask_raw) if ask_raw else None
        mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None

        edge, side = _realizable_edge(model, bid, ask)

        idx = fetch_event_index(event.get("ticker", ""))
        hours_left = (expiry_dt - now).total_seconds() / 3600
        if hours_left < 1:
            t_str = f"{hours_left*60:.0f}m"
        elif hours_left < 48:
            t_str = f"{hours_left:.1f}h"
        else:
            days = hours_left / 24
            t_str = f"{days:.1f}d"

        expiry_str = expiry_dt.strftime("%m-%d %H:%M")

        tbl.add_row(
            sym,
            f"${strike:,.0f}",
            expiry_str,
            t_str,
            idx,
            _fmt_price(bid),
            _fmt_price(ask),
            _fmt_price(mid),
            _fmt_price(model),
            _edge_text(edge, side, bid, ask),
        )

    if n_hidden > 0:
        tbl.add_section()
        tbl.add_row(
            Text(f"  {n_hidden} far OTM/ITM hidden (model outside [{MODEL_FILTER:.2f}, {1-MODEL_FILTER:.2f}])", style="dim"),
            *[""] * 9,
        )

    return tbl


def build_header(spot: float | None, bates_params: BatesParams, n_contracts: int, last_updated: str, error_msg: str | None) -> Panel:
    spot_str = f"${spot:,.2f}" if spot is not None else "[red]unavailable[/red]"
    src = bates_params.calibration_source
    cal_at = bates_params.calibrated_at

    src_style = "green" if src != "fallback-defaults" else "red"
    atm_vol = bates_params.v0 ** 0.5

    lines = [
        f"BTC Spot (Gemini): [bold]{spot_str}[/bold]   "
        f"Params: [{src_style}]{src}[/{src_style}] @ {cal_at}   "
        f"σ₀={atm_vol:.1%}  λ={bates_params.lam:.2f}",
        f"Active contracts: {n_contracts}   Last updated: {last_updated}",
    ]
    if src == "fallback-defaults":
        lines.append("[red]WARNING: Using fallback Bates params — run implied calibration for accurate prices[/red]")
    if error_msg:
        lines.append(f"[red]API error: {error_msg}[/red]")

    return Panel("\n".join(lines), title="[bold cyan]Gemini BTC Prediction Market Pricer[/bold cyan]", border_style="cyan")


def generate_display(bates_params: BatesParams):
    """Fetch live data and build the full Rich renderable."""
    now = datetime.datetime.now(datetime.timezone.utc)
    updated = now.strftime("%H:%M:%S UTC")
    error_msg = None

    spot = fetch_spot_price()
    if spot is None:
        error_msg = "Could not fetch BTC spot price"
        spot = bates_params.S if bates_params.S > 0 else 90000.0  # last known

    events = fetch_active_events()
    n_contracts = sum(len(e.get("contracts", [])) for e in events)

    header = build_header(spot, bates_params, n_contracts, updated, error_msg)

    if not events:
        body = Panel("[yellow]No active BTC events found[/yellow]", border_style="dim")
        return Columns([header, body], expand=True)  # stacked via list below

    renderables = [header]
    for event in events:
        tbl = build_event_table(event, bates_params, spot, now)
        renderables.append(tbl)

    # Return a group (list of renderables printed sequentially)
    from rich.console import Group
    return Group(*renderables)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Live Gemini BTC prediction market pricer dashboard")
    parser.add_argument("--params", default=DEFAULT_PARAMS_PATH,
                        help="Path to BatesParams JSON (default: data/deribit/bates_params_implied.json)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Refresh interval in seconds (default: 5)")
    args = parser.parse_args()

    # Load Bates params
    params_path = os.path.abspath(args.params)
    if not os.path.exists(params_path) and os.path.exists(FALLBACK_PARAMS_PATH):
        params_path = os.path.abspath(FALLBACK_PARAMS_PATH)

    if os.path.exists(params_path):
        bates_params = BatesParams.load(params_path)
        print(f"Loaded params from {params_path}")
    else:
        bates_params = FALLBACK_PARAMS
        print(f"[WARNING] Params file not found at {params_path} — using fallback defaults")

    console = Console()

    console.print(f"\n[cyan]Starting live dashboard (refresh: {args.interval:.0f}s, Ctrl+C to quit)[/cyan]\n")

    with Live(generate_display(bates_params), console=console, refresh_per_second=4, screen=False) as live:
        try:
            while True:
                time.sleep(args.interval)
                live.update(generate_display(bates_params))
        except KeyboardInterrupt:
            pass

    console.print("\n[dim]Dashboard stopped.[/dim]")


if __name__ == "__main__":
    main()
