"""
Live paper-trading runner for Gemini BTC prediction markets.

Paper execution rule:
  if the best quoted price clears the edge threshold, assume full fill at that
  top-of-book price and record the position locally.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from types import SimpleNamespace

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SRC_DIR, "pricer"))
sys.path.insert(0, SRC_DIR)

import bates_pricer as bp
from calibration.params import BatesParams
from strategy.config import StrategyConfig
from strategy.position_log import Position, PositionLog
from strategy.scenario_risk import (
    ScenarioEvaluationCache,
    evaluate_candidate_quantity,
    scenario_risk_is_active,
)
from strategy.signal import generate_signal
from strategy.sizing import flat_size, kelly_size, max_loss_per_contract


BASE = "https://api.gemini.com"
INDEX_RE = re.compile(r"(GRR-KAIKO_BTCUSD_1S|KK_BRR_BTCUSD)")
TICKER_RE = re.compile(r"^GEMI-BTC(\d{10})-HI(\d+)$")
PROJECT_ROOT = os.path.abspath(os.path.join(SRC_DIR, ".."))
DEFAULT_PARAMS_FALLBACK = os.path.join(PROJECT_ROOT, "data", "bates_params_implied.json")
DEFAULT_TRADE_LOG = os.path.join(PROJECT_ROOT, "data", "strategy", "paper_trades.csv")


@dataclass
class BookQuote:
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float


def get_json(url: str, timeout: int = 10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def resolve_params_path(cfg: StrategyConfig) -> str:
    preferred = os.path.join(PROJECT_ROOT, cfg.params_path)
    if os.path.exists(preferred):
        return preferred
    if os.path.exists(DEFAULT_PARAMS_FALLBACK):
        return DEFAULT_PARAMS_FALLBACK
    raise FileNotFoundError(
        f"Could not find params file at {preferred} or {DEFAULT_PARAMS_FALLBACK}"
    )


def load_params(cfg: StrategyConfig) -> tuple[BatesParams, str]:
    path = resolve_params_path(cfg)
    return BatesParams.load(path), path


def fetch_spot_price() -> float:
    data = get_json(f"{BASE}/v1/pubticker/BTCUSD")
    return float(data["last"])


def fetch_active_events() -> list[dict]:
    data = get_json(f"{BASE}/v1/prediction-markets/events?limit=100&search=BTC&status=active")
    return data.get("data", [])


def fetch_order_book(symbol: str) -> BookQuote:
    data = get_json(f"{BASE}/v1/book/{symbol}?limit_bids=1&limit_asks=1")
    bids = data.get("bids", [])
    asks = data.get("asks", [])

    bid = float(bids[0]["price"]) if bids else None
    bid_size = float(bids[0].get("amount", 0.0)) if bids else 0.0
    ask = float(asks[0]["price"]) if asks else None
    ask_size = float(asks[0].get("amount", 0.0)) if asks else 0.0
    return BookQuote(bid=bid, bid_size=bid_size, ask=ask, ask_size=ask_size)


def fetch_event_detail(event_ticker: str) -> dict:
    return get_json(f"{BASE}/v1/prediction-markets/events/{event_ticker}")


def parse_instrument(symbol: str) -> tuple[str, dt.datetime, float] | None:
    match = TICKER_RE.match(symbol)
    if not match:
        return None
    code = match.group(1)
    expiry = dt.datetime(
        int("20" + code[0:2]),
        int(code[2:4]),
        int(code[4:6]),
        int(code[6:8]),
        int(code[8:10]),
        tzinfo=dt.timezone.utc,
    )
    return f"BTC{code}", expiry, float(match.group(2))


def fetch_event_index(event_ticker: str) -> str:
    data = fetch_event_detail(event_ticker)
    idx = "GRR-KAIKO_BTCUSD_1S"
    for contract in data.get("contracts", []):
        desc = contract.get("description", {})
        text = ""
        for block in desc.get("content", []):
            for item in block.get("content", [block]):
                text += item.get("value", "")
        match = INDEX_RE.search(text)
        if match:
            idx = match.group(1)
            break
    return idx


def price_binary_yes(params: BatesParams, strike: float, T_years: float, spot_price: float) -> float:
    pricer_params = params.to_pricer_params()
    pricer_params.S = float(spot_price)
    return float(bp.binary_call(strike, T_years, pricer_params, N=256))


def settlement_pnl_per_contract(side: str, entry_price: float, outcome: int) -> float:
    if side == "buy":
        return float(outcome) - entry_price
    if side == "sell":
        return entry_price - float(outcome)
    raise ValueError(f"unsupported side: {side!r}")


def desired_quantity(
    cfg: StrategyConfig,
    side: str,
    model_price: float,
    entry_price: float,
    capital_remaining: float,
) -> int:
    if cfg.sizing_mode == "kelly":
        qty = kelly_size(
            cfg=cfg,
            model_price=model_price,
            entry_price=entry_price,
            current_portfolio_value=max(capital_remaining, 0.0),
            side=side,
        )
    else:
        qty = flat_size(cfg, entry_price, side=side)

    risk_per_contract = max_loss_per_contract(entry_price, side)
    max_qty = int(capital_remaining // risk_per_contract) if risk_per_contract > 0 else 0
    return max(0, min(qty, max_qty))


def export_trade_blotter(path: str, positions: list[Position], fee_per_contract: float) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "instrument",
                "event_ticker",
                "settlement_index",
                "status",
                "side",
                "outcome_field",
                "quantity",
                "entry_price",
                "entry_model_price",
                "edge_at_entry",
                "spot_at_entry",
                "bid_at_entry",
                "ask_at_entry",
                "bid_size_at_entry",
                "ask_size_at_entry",
                "entry_time",
                "expiry_time",
                "settlement_time",
                "settlement_outcome",
                "entry_notional_usd",
                "max_loss_usd",
                "gross_pnl_per_contract",
                "gross_total_pnl",
                "fees_per_contract",
                "fees_total",
                "net_total_pnl",
                "order_id",
                "params_path",
            ]
        )
        for position in positions:
            entry_notional = position.quantity * position.entry_price
            max_loss_usd = position.quantity * max_loss_per_contract(
                position.entry_price,
                position.side,
            )
            gross_pnl_per_contract = ""
            gross_total_pnl = ""
            net_total_pnl = ""
            if position.settlement_outcome is not None:
                gross_pnl_per_contract = round(
                    settlement_pnl_per_contract(
                        position.side,
                        position.entry_price,
                        position.settlement_outcome,
                    ),
                    6,
                )
                gross_total_pnl = round(gross_pnl_per_contract * position.quantity, 6)
                net_total_pnl = round(gross_total_pnl - (position.quantity * fee_per_contract), 6)

            writer.writerow(
                [
                    position.instrument,
                    position.event_ticker,
                    position.settlement_index,
                    position.status,
                    position.side,
                    position.outcome,
                    position.quantity,
                    position.entry_price,
                    position.entry_model_price,
                    position.edge_at_entry,
                    position.spot_at_entry,
                    position.bid_at_entry,
                    position.ask_at_entry,
                    position.bid_size_at_entry,
                    position.ask_size_at_entry,
                    position.entry_time,
                    position.expiry_time,
                    position.settlement_time,
                    position.settlement_outcome,
                    round(entry_notional, 6),
                    round(max_loss_usd, 6),
                    gross_pnl_per_contract,
                    gross_total_pnl,
                    fee_per_contract,
                    round(position.quantity * fee_per_contract, 6),
                    net_total_pnl,
                    position.order_id,
                    position.params_path,
                ]
            )


def settle_positions(position_log: PositionLog) -> list[str]:
    now = dt.datetime.now(dt.timezone.utc)
    settled: list[str] = []
    for position in position_log.open_positions():
        expiry_dt = dt.datetime.fromisoformat(position.expiry_time.replace("Z", "+00:00"))
        if now < expiry_dt:
            continue

        detail = fetch_event_detail(position.event_ticker)
        outcome = None
        for contract in detail.get("contracts", []):
            if contract.get("instrumentSymbol") != position.instrument:
                continue
            if contract.get("status") != "settled":
                break
            resolution_side = contract.get("resolutionSide")
            if resolution_side == "yes":
                outcome = 1
            elif resolution_side == "no":
                outcome = 0
            break

        if outcome is None:
            continue

        updated = position_log.update_settled(
            instrument=position.instrument,
            outcome=outcome,
            settlement_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if updated is not None:
            settled.append(position.instrument)
    return settled


def run_paper(cfg: StrategyConfig, trade_log_path: str = DEFAULT_TRADE_LOG, once: bool = False) -> None:
    position_log = PositionLog(os.path.join(PROJECT_ROOT, cfg.positions_path))
    params, params_path = load_params(cfg)
    trade_log_abs = os.path.join(PROJECT_ROOT, trade_log_path)
    scenario_risk_enabled = scenario_risk_is_active(cfg)
    print(f"Loaded params from {params_path}")

    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            settled = settle_positions(position_log)
            if settled:
                print(f"Settled {len(settled)} paper position(s): {', '.join(settled)}")

            events = fetch_active_events()
            spot = fetch_spot_price()
            open_positions = position_log.open_positions()
            open_instruments = {position.instrument for position in open_positions}
            capital_remaining = cfg.total_capital_usd - position_log.total_exposure_usd()
            scenario_evaluation_cache = ScenarioEvaluationCache()

            print(
                f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] "
                f"active_events={len(events)} open_positions={len(open_positions)} "
                f"capital_remaining=${capital_remaining:.2f}"
            )

            for event in events:
                event_ticker = event.get("ticker", "")
                try:
                    settlement_index = fetch_event_index(event_ticker)
                except Exception as exc:
                    print(f"  SKIP EVENT {event_ticker or '?'} error={exc}")
                    continue

                for contract in event.get("contracts", []):
                    symbol = contract.get("instrumentSymbol", "")
                    if not symbol or (cfg.one_per_instrument and symbol in open_instruments):
                        continue
                    if len(position_log.open_positions()) >= cfg.max_open_positions:
                        break

                    try:
                        parsed = parse_instrument(symbol)
                        if parsed is None:
                            continue
                        _, expiry_dt, strike = parsed
                        T_years = max((expiry_dt - now).total_seconds(), 0.0) / (365.25 * 24 * 3600)
                        if T_years <= 0:
                            continue

                        quote = fetch_order_book(symbol)
                        model_price = price_binary_yes(params, strike, T_years, spot)
                        signal = generate_signal(
                            instrument=symbol,
                            model_price=model_price,
                            bid=quote.bid,
                            ask=quote.ask,
                            strike=strike,
                            expiry_dt=expiry_dt,
                            T_years=T_years,
                            cfg=cfg,
                        )
                        if signal is None:
                            continue

                        qty = desired_quantity(
                            cfg=cfg,
                            side=signal.side,
                            model_price=signal.model_price,
                            entry_price=signal.entry_price,
                            capital_remaining=capital_remaining,
                        )
                        if qty < 1:
                            continue
                        requested_qty = qty

                        scenario_gate = evaluate_candidate_quantity(
                            cfg=cfg,
                            current_positions=open_positions,
                            candidate_position=SimpleNamespace(
                                instrument=symbol,
                                side=signal.side,
                                entry_price=signal.entry_price,
                                event_ticker=event_ticker,
                            ),
                            initial_quantity=qty,
                            params=params,
                            as_of=now,
                            spot_price=spot,
                            cache=scenario_evaluation_cache,
                            params_identifier=params_path,
                        )
                        if scenario_gate.approved_quantity < 1:
                            if scenario_risk_enabled and scenario_gate.decision is not None:
                                print(
                                    f"  SKIP RISK {symbol} side={signal.side} "
                                    f"reasons={' | '.join(scenario_gate.decision.reasons)}"
                                )
                            continue
                        qty = scenario_gate.approved_quantity

                        position = Position(
                            instrument=symbol,
                            event_ticker=event_ticker,
                            side=signal.side,
                            outcome="yes",
                            quantity=qty,
                            entry_price=signal.entry_price,
                            entry_model_price=signal.model_price,
                            edge_at_entry=signal.edge,
                            entry_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            expiry_time=expiry_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            order_id=f"paper-{symbol}-{int(now.timestamp())}",
                            settlement_index=settlement_index,
                            status="open",
                            spot_at_entry=spot,
                            bid_at_entry=quote.bid,
                            ask_at_entry=quote.ask,
                            bid_size_at_entry=quote.bid_size,
                            ask_size_at_entry=quote.ask_size,
                            params_path=params_path,
                        )
                        position_log.add(position)
                        open_positions.append(position)
                        capital_remaining -= qty * max_loss_per_contract(signal.entry_price, signal.side)
                        open_instruments.add(symbol)
                        if qty < requested_qty:
                            print(
                                f"  RISK RESIZE {symbol} qty={requested_qty}->{qty} "
                                f"downside={scenario_gate.comparison.candidate_metrics.terminal_downside:.3f} "
                                f"delta={scenario_gate.comparison.candidate_metrics.terminal_max_abs_delta:.4f}"
                            )
                        flatness_str = (
                            f" flatness={scenario_gate.comparison.candidate_metrics.surface_flatness_terminal:.2f}"
                            if scenario_gate.comparison is not None
                            else ""
                        )
                        print(
                            f"  PAPER FILL {signal.side.upper()} {symbol} qty={qty} "
                            f"entry={signal.entry_price:.3f} model={signal.model_price:.3f} "
                            f"edge={signal.edge:.3f}{flatness_str}"
                        )
                    except Exception as exc:
                        print(f"  SKIP CONTRACT {symbol or '?'} error={exc}")

            export_trade_blotter(
                path=trade_log_abs,
                positions=position_log.load(),
                fee_per_contract=cfg.paper_fee_per_contract,
            )

            if once:
                break
            time.sleep(cfg.poll_interval_seconds)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if once:
                raise
            now = dt.datetime.now(dt.timezone.utc)
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] loop_error={exc}")
            time.sleep(cfg.poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live paper-trading runner for Gemini BTC prediction markets")
    parser.add_argument("--config", help="Optional StrategyConfig JSON path")
    parser.add_argument(
        "--trade-log",
        default=None,
        help="CSV path for the paper trade blotter (defaults to config paper_trades_path)",
    )
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit")
    args = parser.parse_args()

    cfg = StrategyConfig.load(args.config) if args.config else StrategyConfig()
    trade_log_path = args.trade_log or cfg.paper_trades_path
    run_paper(cfg=cfg, trade_log_path=trade_log_path, once=args.once)


if __name__ == "__main__":
    main()
