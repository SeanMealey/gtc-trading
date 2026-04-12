"""
Signal generation — pure function, no I/O, no API calls.

The same logic is reused anywhere the strategy evaluates a live market quote.
"""

from __future__ import annotations
import datetime
from dataclasses import dataclass

from .config import StrategyConfig


@dataclass
class Signal:
    instrument: str
    side: str              # "buy" or "sell"
    edge: float            # model - ask (buy) or bid - model (sell)
    model_price: float
    entry_price: float     # ask price for buys, bid price for sells
    strike: float
    expiry_dt: datetime.datetime
    T_years: float


def generate_signal(
    instrument: str,
    model_price: float,
    bid: float | None,
    ask: float | None,
    strike: float,
    expiry_dt: datetime.datetime,
    T_years: float,
    cfg: StrategyConfig,
) -> Signal | None:
    """
    Return a Signal if all entry conditions are satisfied, else None.

    Conditions checked in order:
      1. Model in tradeable zone [model_min, model_max]
      2. T <= max_t_days
      3. Two-sided market (if require_two_sided)
      4. Side-aware entry price guardrails
      5. BUY: model > ask + buy_min_edge
         SELL: bid > model + sell_min_edge
    """
    if model_price is None:
        return None

    # 1. Model zone — avoid extreme-probability contracts
    if not (cfg.model_min < model_price < cfg.model_max):
        return None

    # 2. Time to expiry cap
    if T_years > cfg.max_t_days / 365.25:
        return None

    # 3. Two-sided market requirement
    if cfg.require_two_sided and (bid is None or ask is None):
        return None

    buy_min_edge = cfg.effective_buy_min_edge()
    sell_min_edge = cfg.effective_sell_min_edge()

    # 4a/5a. BUY signal — require ask floor, then positive edge vs ask
    if (
        ask is not None
        and ask >= cfg.buy_min_price
        and model_price >= ask + buy_min_edge
    ):
        return Signal(
            instrument=instrument,
            side="buy",
            edge=model_price - ask,
            model_price=model_price,
            entry_price=ask,
            strike=strike,
            expiry_dt=expiry_dt,
            T_years=T_years,
        )

    # 4b/5b. SELL signal — require bid cap, then positive edge vs bid
    if (
        bid is not None
        and bid <= cfg.sell_max_price
        and bid >= model_price + sell_min_edge
    ):
        return Signal(
            instrument=instrument,
            side="sell",
            edge=bid - model_price,
            model_price=model_price,
            entry_price=bid,
            strike=strike,
            expiry_dt=expiry_dt,
            T_years=T_years,
        )

    return None
