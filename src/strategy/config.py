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
    buy_min_edge: float | None = None
    sell_min_edge: float | None = None
    model_min: float = 0.15         # reject if model < model_min (deep ITM for seller)
    model_max: float = 0.85         # reject if model > model_max (deep ITM for buyer)
    buy_min_price: float = 0.15     # reject buys below this ask price
    sell_max_price: float = 0.85    # reject sells above this bid price
    max_t_days: float = 7.0         # reject contracts with T > max_t_days until expiry

    # ── Liquidity Gate ────────────────────────────────────────────────────────
    require_two_sided: bool = True  # require both bid AND ask to be present
    min_total_shares: int = 5       # minimum totalShares on the contract

    # ── Sizing ────────────────────────────────────────────────────────────────
    sizing_mode: str = "flat"       # "flat" or "kelly"
    flat_amount_usd: float = 10.0   # USD per trade (flat mode) — configure before trading
    kelly_fraction: float = 0.25    # fraction of full Kelly (quarter-Kelly default)
    total_capital_usd: float = 100.0

    # ── Exposure Limits ───────────────────────────────────────────────────────
    max_open_positions: int = 5     # max simultaneously held positions
    one_per_instrument: bool = True # no doubling into the same instrument
    max_position_pct: float = 0.20  # max fraction of total capital in one position (Kelly)
    enable_scenario_risk: bool = False
    scenario_min_positions: int = 0
    scenario_reduce_size_to_fit: bool = True
    scenario_use_bates_probabilities: bool = True
    scenario_price_range_pct: float = 0.15
    scenario_price_step: float = 250.0
    scenario_time_step_hours: float = 4.0
    scenario_max_surface_flatness: float | None = None
    scenario_max_terminal_negative_cells: int | None = None
    scenario_max_payoff_variance: float | None = None
    scenario_min_expected_pnl: float | None = None
    scenario_min_max_loss: float | None = None
    scenario_max_terminal_downside: float | None = None
    scenario_max_terminal_abs_delta: float | None = None
    scenario_max_terminal_pin_risk: float | None = None
    scenario_pin_risk_window_steps: int = 1
    scenario_require_flatness_improvement: bool = False
    scenario_require_variance_improvement: bool = False
    scenario_require_hole_reduction: bool = False
    scenario_require_downside_improvement: bool = False
    scenario_require_delta_improvement: bool = False
    scenario_require_pin_risk_improvement: bool = False
    scenario_require_expected_pnl_improvement: bool = False

    # ── Inventory Skew ────────────────────────────────────────────────────────
    enable_inventory_skew: bool = False
    inventory_skew_ev_weight: float = 1.0
    inventory_skew_flatness_weight: float = 0.5
    inventory_skew_max_loss_weight: float = 0.5
    inventory_skew_downside_weight: float = 0.25
    inventory_skew_delta_weight: float = 0.25
    inventory_skew_pin_risk_weight: float = 0.5
    inventory_skew_max_edge_credit: float = 0.02
    inventory_skew_max_edge_penalty: float = 0.02
    inventory_skew_require_positive_score: bool = False
    inventory_skew_size_multiplier_min: float = 0.5
    inventory_skew_size_multiplier_max: float = 1.25

    # ── Calibration ───────────────────────────────────────────────────────────
    params_path: str = "data/deribit/bates_params_implied.json"
    params_history_dir: str = "data/deribit/params_history"
    calibration_interval_hours: float = 24.0  # recalibrate every N hours; pauses entries

    # ── State & Logging ───────────────────────────────────────────────────────
    positions_path: str = "data/strategy/positions.json"
    trades_log_path: str = "data/strategy/trades.csv"
    runner_log_path: str = "logs/live_runner.log"

    # ── Loop ──────────────────────────────────────────────────────────────────
    poll_interval_seconds: float = 5.0

    # ── Live Execution & Safety ───────────────────────────────────────────────
    gemini_base_url: str = "https://api.gemini.com"
    request_timeout_seconds: float = 10.0
    submit_orders: bool = False              # gate live order submission
    dry_run: bool = True                     # if True, use ExecutionClient(dry_run=True)
    time_in_force: str = "immediate-or-cancel"

    max_notional_per_order_usd: float = 25.0
    max_total_notional_usd: float = 200.0
    max_quantity_per_order: int = 50
    max_open_positions_live: int = 10
    max_consecutive_api_failures: int = 5
    max_params_age_hours: float = 48.0
    max_book_spread: float = 0.20            # reject contracts where ask - bid > this
    daily_loss_limit_usd: float = 50.0
    daily_filled_notional_cap_usd: float = 500.0
    kill_switch_path: str = "logs/KILL_SWITCH"

    require_state_reconciliation: bool = True
    reconciliation_max_quantity_drift: int = 0  # exact match by default

    log_heartbeat_every_n_loops: int = 12

    # ── Helpers ───────────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    def effective_buy_min_edge(self) -> float:
        return self.min_edge if self.buy_min_edge is None else self.buy_min_edge

    def effective_sell_min_edge(self) -> float:
        return self.min_edge if self.sell_min_edge is None else self.sell_min_edge

    @classmethod
    def load(cls, path: str) -> StrategyConfig:
        with open(path) as f:
            return cls(**json.load(f))
