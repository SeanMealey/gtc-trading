"""
StrategyConfig — all tunable parameters for the BTC prediction market strategy.

All values are set to agreed defaults and can be overridden at construction time
or loaded from a JSON file for reproducibility.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict


@dataclass
class StrategyConfig:
    # ── Signal ────────────────────────────────────────────────────────────────
    min_edge: float = 0.03          # realizable edge required to enter
                                    # BUY: model - ask >= min_edge
                                    # SELL: bid - model >= min_edge
    model_min: float = 0.15         # reject if model < model_min (deep ITM for seller)
    model_max: float = 0.85         # reject if model > model_max (deep ITM for buyer)
    max_t_days: float = 7.0         # reject contracts with T > max_t_days until expiry

    # ── Liquidity Gate ────────────────────────────────────────────────────────
    require_two_sided: bool = True  # require both bid AND ask to be present
    min_total_shares: int = 5       # minimum totalShares on the contract (live)
                                    # in backtest: minimum candle volume

    # ── Sizing ────────────────────────────────────────────────────────────────
    sizing_mode: str = "flat"       # "flat" or "kelly"
    flat_amount_usd: float = 10.0   # USD per trade (flat mode) — configure before trading
    kelly_fraction: float = 0.25    # fraction of full Kelly (quarter-Kelly default)
    total_capital_usd: float = 100.0

    # ── Exposure Limits ───────────────────────────────────────────────────────
    max_open_positions: int = 5     # max simultaneously held positions
    one_per_instrument: bool = True # no doubling into the same instrument
    max_position_pct: float = 0.20  # max fraction of total capital in one position (Kelly)

    # ── Calibration (live runner) ─────────────────────────────────────────────
    params_path: str = "data/deribit/bates_params_implied.json"
    params_history_dir: str = "data/deribit/params_history"
    calibration_interval_hours: float = 24.0  # recalibrate every N hours; pauses entries

    # ── State & Logging ───────────────────────────────────────────────────────
    positions_path: str = "data/strategy/positions.json"
    trades_log_path: str = "data/strategy/trades.csv"
    paper_trades_path: str = "data/strategy/paper_trades.csv"
    paper_fee_per_contract: float = 0.02

    # ── Loop (live runner) ────────────────────────────────────────────────────
    poll_interval_seconds: float = 5.0

    # ── Backtest ──────────────────────────────────────────────────────────────
    backtest_spread_half: float = 0.03   # synthetic half-spread applied to candle close
                                          # bid = close - spread_half, ask = close + spread_half
    backtest_entry: str = "first"         # "first" = enter on first signal per instrument
                                          # "best"  = enter on highest-edge candle (look-ahead, optimistic)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: str) -> StrategyConfig:
        with open(path) as f:
            return cls(**json.load(f))
