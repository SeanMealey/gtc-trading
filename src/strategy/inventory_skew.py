from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.config import StrategyConfig
from strategy.scenario_matrix import ScenarioComparison


@dataclass(frozen=True)
class InventorySkewDecision:
    raw_edge: float
    base_required_edge: float
    effective_required_edge: float
    inventory_adjustment: float
    score: float
    passes_inventory_filter: bool
    requested_quantity: int
    adjusted_quantity: int
    size_multiplier: float
    delta_expected_pnl: float
    delta_flatness: float
    delta_max_loss: float
    delta_downside: float
    delta_delta: float
    delta_pin_risk: float
    reasons: tuple[str, ...]


def inventory_skew_is_active(cfg: StrategyConfig) -> bool:
    return bool(cfg.enable_inventory_skew)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _size_multiplier_from_adjustment(adjustment: float, cfg: StrategyConfig) -> float:
    if adjustment >= 0:
        max_credit = max(cfg.inventory_skew_max_edge_credit, 1e-12)
        ratio = _clamp(adjustment / max_credit, 0.0, 1.0)
        return 1.0 + ratio * max(cfg.inventory_skew_size_multiplier_max - 1.0, 0.0)

    max_penalty = max(cfg.inventory_skew_max_edge_penalty, 1e-12)
    ratio = _clamp((-adjustment) / max_penalty, 0.0, 1.0)
    return 1.0 - ratio * max(1.0 - cfg.inventory_skew_size_multiplier_min, 0.0)


def evaluate_inventory_skew(
    *,
    cfg: StrategyConfig,
    raw_edge: float,
    base_required_edge: float,
    comparison: ScenarioComparison,
    requested_quantity: int,
) -> InventorySkewDecision:
    if requested_quantity < 0:
        raise ValueError("requested_quantity must be non-negative")

    current = comparison.current_metrics
    candidate = comparison.candidate_metrics

    delta_expected_pnl = 0.0
    if current.expected_pnl is not None and candidate.expected_pnl is not None:
        delta_expected_pnl = candidate.expected_pnl - current.expected_pnl

    delta_flatness = current.surface_flatness_terminal - candidate.surface_flatness_terminal
    delta_max_loss = candidate.max_loss - current.max_loss
    delta_downside = current.terminal_downside - candidate.terminal_downside
    delta_delta = current.terminal_max_abs_delta - candidate.terminal_max_abs_delta
    delta_pin_risk = current.terminal_pin_risk - candidate.terminal_pin_risk

    score = (
        cfg.inventory_skew_ev_weight * delta_expected_pnl
        + cfg.inventory_skew_flatness_weight * delta_flatness
        + cfg.inventory_skew_max_loss_weight * delta_max_loss
        + cfg.inventory_skew_downside_weight * delta_downside
        + cfg.inventory_skew_delta_weight * delta_delta
        + cfg.inventory_skew_pin_risk_weight * delta_pin_risk
    )
    inventory_adjustment = _clamp(
        score,
        -cfg.inventory_skew_max_edge_penalty,
        cfg.inventory_skew_max_edge_credit,
    )
    effective_required_edge = max(base_required_edge - inventory_adjustment, 0.0)
    size_multiplier = _size_multiplier_from_adjustment(inventory_adjustment, cfg)
    adjusted_quantity = 0
    if requested_quantity > 0 and size_multiplier > 0:
        adjusted_quantity = max(1, math.floor(requested_quantity * size_multiplier))

    reasons: list[str] = []
    if score > 0:
        reasons.append(
            "inventory profile improved "
            f"(ev={delta_expected_pnl:+.4f}, flatness={delta_flatness:+.4f}, "
            f"max_loss={delta_max_loss:+.4f}, downside={delta_downside:+.4f}, "
            f"delta={delta_delta:+.6f}, pin_risk={delta_pin_risk:+.4f})"
        )
    elif score < 0:
        reasons.append(
            "inventory profile worsened "
            f"(ev={delta_expected_pnl:+.4f}, flatness={delta_flatness:+.4f}, "
            f"max_loss={delta_max_loss:+.4f}, downside={delta_downside:+.4f}, "
            f"delta={delta_delta:+.6f}, pin_risk={delta_pin_risk:+.4f})"
        )
    else:
        reasons.append("inventory profile neutral")

    passes_inventory_filter = raw_edge >= effective_required_edge
    if cfg.inventory_skew_require_positive_score and score <= 0:
        passes_inventory_filter = False
        reasons.append("inventory skew requires a positive score")
    if raw_edge < effective_required_edge:
        reasons.append(
            f"raw edge {raw_edge:.4f} below inventory-adjusted threshold {effective_required_edge:.4f}"
        )

    return InventorySkewDecision(
        raw_edge=raw_edge,
        base_required_edge=base_required_edge,
        effective_required_edge=effective_required_edge,
        inventory_adjustment=inventory_adjustment,
        score=score,
        passes_inventory_filter=passes_inventory_filter,
        requested_quantity=requested_quantity,
        adjusted_quantity=adjusted_quantity,
        size_multiplier=size_multiplier,
        delta_expected_pnl=delta_expected_pnl,
        delta_flatness=delta_flatness,
        delta_max_loss=delta_max_loss,
        delta_downside=delta_downside,
        delta_delta=delta_delta,
        delta_pin_risk=delta_pin_risk,
        reasons=tuple(reasons),
    )
