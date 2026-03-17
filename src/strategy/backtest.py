"""
Historical replay backtest for the Gemini BTC prediction-market strategy.

This backtest is intentionally simple:
  - market candles are treated as the tradeable mid/last price proxy
  - a synthetic spread is applied around the candle close
  - entries are taken on the first or best signal per instrument
  - positions are held to settlement

It is useful for validating whether the signal has edge at all, but it is not a
substitute for a full fill-model or paper-trading loop.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
import os
import re
import sys
from dataclasses import dataclass

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if THIS_DIR in sys.path:
    sys.path.remove(THIS_DIR)
sys.path.insert(0, os.path.join(SRC_DIR, "pricer"))
sys.path.insert(0, SRC_DIR)

import bates_pricer as bp
import pandas as pd
from calibration.params import BatesParams
from strategy.config import StrategyConfig
from strategy.scenario_risk import (
    ScenarioEvaluationCache,
    evaluate_candidate_quantity,
    scenario_risk_is_active,
)
from strategy.signal import generate_signal
from strategy.sizing import flat_size, kelly_size, max_loss_per_contract


PROJECT_ROOT = os.path.abspath(os.path.join(SRC_DIR, ".."))
DEFAULT_CANDLES_PATH = os.path.join(
    PROJECT_ROOT, "data", "gemini_prediction_markets", "combined_candles.csv"
)
DEFAULT_SETTLEMENTS_PATH = os.path.join(
    PROJECT_ROOT, "data", "gemini_prediction_markets", "settlements.csv"
)
DEFAULT_SPOT_PATH = os.path.join(
    PROJECT_ROOT, "data", "gemini_spot", "BTCUSD_composite.csv"
)
DEFAULT_SPOT_SUPPLEMENT_PATH = os.path.join(
    PROJECT_ROOT, "data", "gemini_spot", "BTCUSD_binance_supplement_1m.csv"
)
DEFAULT_OUTPUT_PATH = os.path.join(
    PROJECT_ROOT, "data", "strategy", "backtest_trades.csv"
)
FALLBACK_PARAMS_PATH = os.path.join(PROJECT_ROOT, "data", "bates_params_implied.json")
DEFAULT_PARAMS_HISTORY_DIR = os.path.join(PROJECT_ROOT, "data", "deribit", "params_history")
BAR_INTERVAL_MS = 5 * 60 * 1000
INSTRUMENT_RE = re.compile(r"^GEMI-BTC(?P<event>\d{10})-HI(?P<strike>\d+)$")


@dataclass
class CandidateTrade:
    instrument: str
    event_ticker: str
    entry_ts_ms: int
    expiry_ts_ms: int
    side: str
    quantity: int
    entry_price: float
    model_price: float
    edge: float
    strike: float
    spot_at_entry: float
    settlement_outcome: int
    required_risk: float
    bar_volume: float
    params_path: str


@dataclass
class BacktestSummary:
    instruments_with_candles: int
    instruments_with_candidates: int
    trades_executed: int
    buys: int
    sells: int
    wins: int
    losses: int
    gross_pnl: float
    avg_pnl: float
    avg_edge: float
    win_rate: float
    start_capital: float
    ending_equity: float
    scenario_rejections: int
    scenario_resized_trades: int


@dataclass
class HistoricalParams:
    calibrated_at_ms: int
    params_path: str
    params: BatesParams


def parse_instrument(symbol: str) -> tuple[str, dt.datetime, float] | None:
    match = INSTRUMENT_RE.match(symbol)
    if not match:
        return None

    event_code = match.group("event")
    expiry = dt.datetime(
        int("20" + event_code[0:2]),
        int(event_code[2:4]),
        int(event_code[4:6]),
        int(event_code[6:8]),
        int(event_code[8:10]),
        tzinfo=dt.timezone.utc,
    )
    return f"BTC{event_code}", expiry, float(match.group("strike"))


def resolve_params_path(path: str) -> str:
    candidates = [path, FALLBACK_PARAMS_PATH]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        f"Could not find Bates params file. Tried: {', '.join(candidates)}"
    )


def load_historical_params(params_history_dir: str) -> list[HistoricalParams]:
    if not os.path.isdir(params_history_dir):
        return []

    history: list[HistoricalParams] = []
    for name in sorted(os.listdir(params_history_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(params_history_dir, name)
        params = BatesParams.load(path)
        if not params.calibrated_at:
            continue
        calibrated_dt = dt.datetime.fromisoformat(
            params.calibrated_at.replace("Z", "+00:00")
        )
        history.append(
            HistoricalParams(
                calibrated_at_ms=int(calibrated_dt.timestamp() * 1000),
                params_path=path,
                params=params,
            )
        )

    history.sort(key=lambda item: item.calibrated_at_ms)
    return history


def params_for_timestamp(
    history: list[HistoricalParams],
    timestamp_ms: int,
    fallback_params: BatesParams,
    fallback_path: str,
) -> tuple[BatesParams, str] | None:
    if not history:
        return fallback_params, fallback_path

    times = [item.calibrated_at_ms for item in history]
    idx = bisect.bisect_right(times, timestamp_ms) - 1
    if idx < 0:
        return None
    return history[idx].params, history[idx].params_path


def load_spot_history(
    spot_path: str = DEFAULT_SPOT_PATH,
    supplement_path: str = DEFAULT_SPOT_SUPPLEMENT_PATH,
) -> pd.DataFrame:
    spot = pd.read_csv(spot_path, usecols=["timestamp_ms", "close", "source_tf_min"]).copy()
    spot["close"] = spot["close"].astype(float)
    spot["source_tf_min"] = spot["source_tf_min"].fillna(1440).astype(int)

    if os.path.exists(supplement_path):
        supplement = pd.read_csv(
            supplement_path, usecols=["timestamp_ms", "close"]
        ).copy()
        supplement["close"] = supplement["close"].astype(float)
        supplement["source_tf_min"] = 1
        spot = pd.concat([spot, supplement], ignore_index=True)

    spot = (
        spot.sort_values(["timestamp_ms", "source_tf_min"])
        .drop_duplicates(subset=["timestamp_ms"], keep="first")
        .sort_values("timestamp_ms")
        .reset_index(drop=True)
    )
    return spot


def spot_lookup(spot_times, spot_prices, timestamp_ms: int) -> float | None:
    idx = spot_times.searchsorted(timestamp_ms, side="right") - 1
    if idx < 0:
        return None
    return float(spot_prices[idx])


def price_binary_yes(
    params: BatesParams,
    strike: float,
    T_years: float,
    spot_price: float,
) -> float:
    pricer_params = params.to_pricer_params()
    pricer_params.S = float(spot_price)
    return float(bp.binary_call(strike, T_years, pricer_params, N=256))


def settlement_pnl_per_contract(side: str, entry_price: float, outcome: int) -> float:
    if side == "buy":
        return float(outcome) - entry_price
    if side == "sell":
        return entry_price - float(outcome)
    raise ValueError(f"unsupported side: {side!r}")


def select_candidate_for_instrument(
    instrument: str,
    bars: pd.DataFrame,
    settlement_outcome: int,
    cfg: StrategyConfig,
    spot_times,
    spot_prices,
    params_history: list[HistoricalParams],
    fallback_params: BatesParams,
    fallback_params_path: str,
) -> CandidateTrade | None:
    parsed = parse_instrument(instrument)
    if parsed is None:
        return None

    event_ticker, expiry_dt, strike = parsed
    expiry_ts_ms = int(expiry_dt.timestamp() * 1000)

    best_candidate: CandidateTrade | None = None
    for row in bars.itertuples(index=False):
        entry_ts_ms = int(row.timestamp_ms) + BAR_INTERVAL_MS
        if entry_ts_ms >= expiry_ts_ms:
            continue

        spot_price = spot_lookup(spot_times, spot_prices, entry_ts_ms)
        if spot_price is None or not math.isfinite(spot_price) or spot_price <= 0:
            continue

        entry_dt = dt.datetime.fromtimestamp(entry_ts_ms / 1000, tz=dt.timezone.utc)
        T_years = max((expiry_dt - entry_dt).total_seconds(), 0.0) / (365.25 * 24 * 3600)
        if T_years <= 0:
            continue

        params_lookup = params_for_timestamp(
            params_history,
            entry_ts_ms,
            fallback_params,
            fallback_params_path,
        )
        if params_lookup is None:
            continue
        params, params_path = params_lookup
        model_price = price_binary_yes(params, strike, T_years, spot_price)
        close = float(row.close)
        bid = max(0.01, close - cfg.backtest_spread_half)
        ask = min(0.99, close + cfg.backtest_spread_half)

        signal = generate_signal(
            instrument=instrument,
            model_price=model_price,
            bid=bid,
            ask=ask,
            strike=strike,
            expiry_dt=expiry_dt,
            T_years=T_years,
            cfg=cfg,
        )
        if signal is None:
            continue

        candidate = CandidateTrade(
            instrument=instrument,
            event_ticker=event_ticker,
            entry_ts_ms=entry_ts_ms,
            expiry_ts_ms=expiry_ts_ms,
            side=signal.side,
            quantity=0,
            entry_price=signal.entry_price,
            model_price=signal.model_price,
            edge=signal.edge,
            strike=signal.strike,
            spot_at_entry=spot_price,
            settlement_outcome=settlement_outcome,
            required_risk=0.0,
            bar_volume=float(row.volume),
            params_path=params_path,
        )

        if cfg.backtest_entry == "first":
            return candidate
        if best_candidate is None or candidate.edge > best_candidate.edge:
            best_candidate = candidate

    return best_candidate


def desired_quantity(
    candidate: CandidateTrade,
    cfg: StrategyConfig,
    portfolio_value: float,
) -> int:
    if cfg.sizing_mode == "kelly":
        return kelly_size(
            cfg=cfg,
            model_price=candidate.model_price,
            entry_price=candidate.entry_price,
            current_portfolio_value=portfolio_value,
            side=candidate.side,
        )
    return flat_size(cfg, candidate.entry_price, side=candidate.side)


def settle_matured_positions(
    open_positions: list[dict],
    current_ts_ms: int,
) -> tuple[list[dict], float]:
    remaining = []
    realized = 0.0
    for position in open_positions:
        if position["expiry_ts_ms"] <= current_ts_ms:
            realized += position["total_pnl"]
        else:
            remaining.append(position)
    return remaining, realized


def run_backtest(
    cfg: StrategyConfig,
    candles_path: str = DEFAULT_CANDLES_PATH,
    settlements_path: str = DEFAULT_SETTLEMENTS_PATH,
    spot_path: str = DEFAULT_SPOT_PATH,
    spot_supplement_path: str = DEFAULT_SPOT_SUPPLEMENT_PATH,
    output_path: str = DEFAULT_OUTPUT_PATH,
) -> tuple[pd.DataFrame, BacktestSummary]:
    params_path = resolve_params_path(os.path.join(PROJECT_ROOT, cfg.params_path))
    params = BatesParams.load(params_path)
    params_cache: dict[str, BatesParams] = {params_path: params}
    params_history_dir = os.path.join(PROJECT_ROOT, cfg.params_history_dir)
    params_history = load_historical_params(params_history_dir)

    settlements = pd.read_csv(
        settlements_path,
        usecols=["instrument", "event_ticker", "outcome"],
    )
    settlements = settlements[settlements["outcome"].isin([0, 1])].copy()
    settlement_map = dict(
        zip(settlements["instrument"], settlements["outcome"].astype(int), strict=False)
    )

    candles = pd.read_csv(
        candles_path,
        usecols=["instrument", "timestamp_ms", "close", "volume"],
    )
    candles = candles[candles["instrument"].isin(settlement_map)].copy()
    candles["volume"] = candles["volume"].astype(float)
    candles = candles[candles["volume"] >= cfg.min_total_shares].copy()
    candles.sort_values(["instrument", "timestamp_ms"], inplace=True)

    spot = load_spot_history(spot_path=spot_path, supplement_path=spot_supplement_path)
    spot_times = spot["timestamp_ms"].to_numpy()
    spot_prices = spot["close"].to_numpy()

    candidates: list[CandidateTrade] = []
    for instrument, group in candles.groupby("instrument", sort=False):
        candidate = select_candidate_for_instrument(
            instrument=instrument,
            bars=group,
            settlement_outcome=settlement_map[instrument],
            cfg=cfg,
            spot_times=spot_times,
            spot_prices=spot_prices,
            params_history=params_history,
            fallback_params=params,
            fallback_params_path=params_path,
        )
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(key=lambda item: item.entry_ts_ms)

    executed_rows: list[dict] = []
    open_positions: list[dict] = []
    realized_pnl = 0.0
    scenario_rejections = 0
    scenario_resized_trades = 0
    scenario_risk_enabled = scenario_risk_is_active(cfg)
    scenario_evaluation_cache = ScenarioEvaluationCache()

    for candidate in candidates:
        open_positions, matured_pnl = settle_matured_positions(
            open_positions, candidate.entry_ts_ms
        )
        realized_pnl += matured_pnl
        portfolio_value = cfg.total_capital_usd + realized_pnl
        reserved_risk = sum(position["required_risk"] for position in open_positions)
        available_risk = max(portfolio_value - reserved_risk, 0.0)

        if len(open_positions) >= cfg.max_open_positions:
            continue

        qty = desired_quantity(candidate, cfg, portfolio_value)
        risk_per_contract = max_loss_per_contract(candidate.entry_price, candidate.side)
        max_by_available_risk = math.floor(available_risk / risk_per_contract)
        qty = min(qty, max_by_available_risk)
        if qty < 1:
            continue
        requested_qty = qty
        scenario_params = params_cache.get(candidate.params_path)
        if scenario_params is None:
            scenario_params = BatesParams.load(candidate.params_path)
            params_cache[candidate.params_path] = scenario_params

        scenario_gate = evaluate_candidate_quantity(
            cfg=cfg,
            current_positions=[position["scenario_position"] for position in open_positions],
            candidate_position=candidate,
            initial_quantity=qty,
            params=scenario_params,
            as_of=dt.datetime.fromtimestamp(candidate.entry_ts_ms / 1000, tz=dt.timezone.utc),
            spot_price=candidate.spot_at_entry,
            cache=scenario_evaluation_cache,
            params_identifier=candidate.params_path,
        )
        if scenario_gate.approved_quantity < 1:
            if scenario_risk_enabled:
                scenario_rejections += 1
            continue
        if scenario_gate.approved_quantity < qty:
            scenario_resized_trades += 1
        qty = scenario_gate.approved_quantity

        pnl_per_contract = settlement_pnl_per_contract(
            candidate.side,
            candidate.entry_price,
            candidate.settlement_outcome,
        )
        total_pnl = pnl_per_contract * qty
        required_risk = risk_per_contract * qty

        row = {
            "instrument": candidate.instrument,
            "event_ticker": candidate.event_ticker,
            "entry_time_utc": dt.datetime.fromtimestamp(
                candidate.entry_ts_ms / 1000, tz=dt.timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "expiry_time_utc": dt.datetime.fromtimestamp(
                candidate.expiry_ts_ms / 1000, tz=dt.timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "side": candidate.side,
            "quantity": qty,
            "entry_price": round(candidate.entry_price, 6),
            "model_price": round(candidate.model_price, 6),
            "edge_at_entry": round(candidate.edge, 6),
            "spot_at_entry": round(candidate.spot_at_entry, 2),
            "strike": candidate.strike,
            "bar_volume": candidate.bar_volume,
            "required_risk": round(required_risk, 6),
            "settlement_outcome": candidate.settlement_outcome,
            "pnl_per_contract": round(pnl_per_contract, 6),
            "total_pnl": round(total_pnl, 6),
            "sizing_mode": cfg.sizing_mode,
            "params_path": candidate.params_path,
            "scenario_qty_requested": int(requested_qty),
            "scenario_qty_approved": int(qty),
            "scenario_terminal_flatness": (
                round(scenario_gate.comparison.candidate_metrics.surface_flatness_terminal, 6)
                if scenario_gate.comparison is not None
                else None
            ),
            "scenario_terminal_negative_cells": (
                int(scenario_gate.comparison.candidate_metrics.terminal_negative_cells)
                if scenario_gate.comparison is not None
                else None
            ),
            "scenario_terminal_downside": (
                round(scenario_gate.comparison.candidate_metrics.terminal_downside, 6)
                if scenario_gate.comparison is not None
                else None
            ),
            "scenario_terminal_abs_delta": (
                round(scenario_gate.comparison.candidate_metrics.terminal_max_abs_delta, 6)
                if scenario_gate.comparison is not None
                else None
            ),
            "scenario_payoff_variance": (
                round(scenario_gate.comparison.candidate_metrics.payoff_variance, 6)
                if scenario_gate.comparison is not None
                else None
            ),
            "scenario_expected_pnl": (
                round(scenario_gate.comparison.candidate_metrics.expected_pnl, 6)
                if scenario_gate.comparison is not None
                and scenario_gate.comparison.candidate_metrics.expected_pnl is not None
                else None
            ),
            "scenario_max_loss": (
                round(scenario_gate.comparison.candidate_metrics.max_loss, 6)
                if scenario_gate.comparison is not None
                else None
            ),
            "scenario_reasons": (
                "|".join(scenario_gate.decision.reasons)
                if scenario_gate.decision is not None
                else ""
            ),
        }
        executed_rows.append(row)
        open_positions.append(
            {
                "expiry_ts_ms": candidate.expiry_ts_ms,
                "required_risk": required_risk,
                "total_pnl": total_pnl,
                "scenario_position": CandidateTrade(
                    instrument=candidate.instrument,
                    event_ticker=candidate.event_ticker,
                    entry_ts_ms=candidate.entry_ts_ms,
                    expiry_ts_ms=candidate.expiry_ts_ms,
                    side=candidate.side,
                    quantity=qty,
                    entry_price=candidate.entry_price,
                    model_price=candidate.model_price,
                    edge=candidate.edge,
                    strike=candidate.strike,
                    spot_at_entry=candidate.spot_at_entry,
                    settlement_outcome=candidate.settlement_outcome,
                    required_risk=required_risk,
                    bar_volume=candidate.bar_volume,
                    params_path=candidate.params_path,
                ),
            }
        )

    if open_positions:
        realized_pnl += sum(position["total_pnl"] for position in open_positions)

    trades = pd.DataFrame(executed_rows)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    trades.to_csv(output_path, index=False)

    trade_count = len(trades)
    gross_pnl = float(trades["total_pnl"].sum()) if trade_count else 0.0
    wins = int((trades["total_pnl"] > 0).sum()) if trade_count else 0
    losses = int((trades["total_pnl"] < 0).sum()) if trade_count else 0

    summary = BacktestSummary(
        instruments_with_candles=int(candles["instrument"].nunique()),
        instruments_with_candidates=len(candidates),
        trades_executed=trade_count,
        buys=int((trades["side"] == "buy").sum()) if trade_count else 0,
        sells=int((trades["side"] == "sell").sum()) if trade_count else 0,
        wins=wins,
        losses=losses,
        gross_pnl=gross_pnl,
        avg_pnl=float(trades["total_pnl"].mean()) if trade_count else 0.0,
        avg_edge=float(trades["edge_at_entry"].mean()) if trade_count else 0.0,
        win_rate=(wins / trade_count) if trade_count else 0.0,
        start_capital=cfg.total_capital_usd,
        ending_equity=cfg.total_capital_usd + gross_pnl,
        scenario_rejections=scenario_rejections,
        scenario_resized_trades=scenario_resized_trades,
    )
    return trades, summary


def print_summary(summary: BacktestSummary, output_path: str) -> None:
    print("Backtest summary")
    print(f"  instruments with candles:  {summary.instruments_with_candles}")
    print(f"  instruments with signals:  {summary.instruments_with_candidates}")
    print(f"  trades executed:          {summary.trades_executed}")
    print(f"  buys / sells:             {summary.buys} / {summary.sells}")
    print(f"  wins / losses:            {summary.wins} / {summary.losses}")
    print(f"  win rate:                 {summary.win_rate:.1%}")
    print(f"  gross pnl:                ${summary.gross_pnl:.2f}")
    print(f"  avg pnl per trade:        ${summary.avg_pnl:.2f}")
    print(f"  avg edge at entry:        {summary.avg_edge:.4f}")
    print(f"  start capital:            ${summary.start_capital:.2f}")
    print(f"  ending equity:            ${summary.ending_equity:.2f}")
    print(f"  scenario rejections:      {summary.scenario_rejections}")
    print(f"  scenario resized trades:  {summary.scenario_resized_trades}")
    print(f"  trade log:                {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay backtest for Gemini BTC prediction markets")
    parser.add_argument("--config", help="Optional StrategyConfig JSON path")
    parser.add_argument("--candles", default=DEFAULT_CANDLES_PATH, help="Combined candles CSV")
    parser.add_argument("--settlements", default=DEFAULT_SETTLEMENTS_PATH, help="Settlements CSV")
    parser.add_argument("--spot", default=DEFAULT_SPOT_PATH, help="Gemini spot composite CSV")
    parser.add_argument(
        "--params-history-dir",
        default=None,
        help="Optional directory of historical Bates params JSON files",
    )
    parser.add_argument(
        "--spot-supplement",
        default=DEFAULT_SPOT_SUPPLEMENT_PATH,
        help="Binance 1m supplement CSV",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Trade log CSV output")
    args = parser.parse_args()

    cfg = StrategyConfig.load(args.config) if args.config else StrategyConfig()
    if args.params_history_dir:
        cfg.params_history_dir = args.params_history_dir
    trades, summary = run_backtest(
        cfg=cfg,
        candles_path=args.candles,
        settlements_path=args.settlements,
        spot_path=args.spot,
        spot_supplement_path=args.spot_supplement,
        output_path=args.output,
    )
    print_summary(summary, args.output)

    if not trades.empty:
        print("\nTop 10 trades by pnl")
        cols = [
            "instrument",
            "side",
            "quantity",
            "entry_price",
            "model_price",
            "edge_at_entry",
            "settlement_outcome",
            "total_pnl",
        ]
        print(trades.sort_values("total_pnl", ascending=False)[cols].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
