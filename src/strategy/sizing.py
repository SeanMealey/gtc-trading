"""
Position sizing helpers for binary-option trades.
"""

from __future__ import annotations

import math

from .config import StrategyConfig


def max_loss_per_contract(entry_price: float, side: str) -> float:
    """
    Worst-case loss per contract for a YES position.

    buy YES  -> lose the premium paid
    sell YES -> lose 1 - premium received
    """
    if side == "buy":
        return max(entry_price, 1e-9)
    if side == "sell":
        return max(1.0 - entry_price, 1e-9)
    raise ValueError(f"unsupported side: {side!r}")


def flat_size(cfg: StrategyConfig, entry_price: float, side: str = "buy") -> int:
    """
    Flat risk sizing in USD per trade.
    """
    risk_per_contract = max_loss_per_contract(entry_price, side)
    return max(1, math.floor(cfg.flat_amount_usd / risk_per_contract))


def kelly_size(
    cfg: StrategyConfig,
    model_price: float,
    entry_price: float,
    current_portfolio_value: float,
    side: str = "buy",
) -> int:
    """
    Fractional Kelly sizing for a binary contract.

    For YES buys:
      win probability = model_price
      risked capital  = entry_price

    For YES sells:
      equivalent to buying NO at price 1 - entry_price
      win probability = 1 - model_price
      risked capital  = 1 - entry_price
    """
    if current_portfolio_value <= 0:
        return 0

    if side == "buy":
        p_win = min(max(model_price, 1e-9), 1.0 - 1e-9)
        stake = max(entry_price, 1e-9)
        payout_profit = 1.0 - entry_price
    elif side == "sell":
        p_win = min(max(1.0 - model_price, 1e-9), 1.0 - 1e-9)
        stake = max(1.0 - entry_price, 1e-9)
        payout_profit = max(entry_price, 1e-9)
    else:
        raise ValueError(f"unsupported side: {side!r}")

    b = payout_profit / stake
    if b <= 0:
        return 0

    f_star = p_win - (1.0 - p_win) / b
    if f_star <= 0:
        return 0

    allocated_usd = cfg.kelly_fraction * f_star * current_portfolio_value
    max_allocated_usd = cfg.max_position_pct * current_portfolio_value
    allocated_usd = min(allocated_usd, max_allocated_usd)

    return max(1, math.floor(allocated_usd / stake))
