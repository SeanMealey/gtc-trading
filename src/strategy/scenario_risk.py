from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Iterable

from calibration.params import BatesParams
from strategy.config import StrategyConfig
from strategy.scenario_matrix import (
    ScenarioComparison,
    ScenarioContract,
    ScenarioDecision,
    ScenarioRiskLimits,
    ScenarioSurface,
    build_portfolio_surface,
    build_scenario_grid,
    compare_surface_addition,
    contract_from_position,
    decide_candidate_trade,
    probability_weights_for_grid,
)


@dataclass(frozen=True)
class ScenarioGateResult:
    approved_quantity: int
    comparison: ScenarioComparison | None
    decision: ScenarioDecision | None


@dataclass
class ScenarioEvaluationCache:
    candidate_surfaces: dict[tuple, ScenarioSurface]
    probability_weights: dict[tuple, object]

    def __init__(self) -> None:
        self.candidate_surfaces = {}
        self.probability_weights = {}

    def _grid_signature(self, prices, evaluation_times) -> tuple:
        return (
            tuple(round(float(price), 8) for price in prices),
            tuple(int(ts.timestamp()) for ts in evaluation_times),
        )

    def _candidate_key(
        self,
        instrument: str,
        side: str,
        entry_price: float,
        quantity: int,
        grid: object,
        params_identifier: str,
    ) -> tuple:
        return (
            instrument,
            side,
            round(float(entry_price), 8),
            int(quantity),
            params_identifier,
            self._grid_signature(grid.prices, grid.evaluation_times),
        )

    def _probability_key(
        self,
        params_identifier: str,
        probability_method: str | None,
        spot_price: float,
        as_of: dt.datetime,
        grid: object,
    ) -> tuple:
        return (
            params_identifier,
            probability_method,
            round(float(spot_price), 8),
            int(as_of.timestamp()),
            self._grid_signature(grid.prices, grid.evaluation_times),
        )


def scenario_limits_from_config(cfg: StrategyConfig) -> ScenarioRiskLimits:
    return ScenarioRiskLimits(
        max_surface_flatness=cfg.scenario_max_surface_flatness,
        max_terminal_negative_cells=cfg.scenario_max_terminal_negative_cells,
        max_payoff_variance=cfg.scenario_max_payoff_variance,
        min_expected_pnl=cfg.scenario_min_expected_pnl,
        min_max_loss=cfg.scenario_min_max_loss,
        max_terminal_downside=cfg.scenario_max_terminal_downside,
        max_terminal_abs_delta=cfg.scenario_max_terminal_abs_delta,
        require_flatness_improvement=cfg.scenario_require_flatness_improvement,
        require_variance_improvement=cfg.scenario_require_variance_improvement,
        require_hole_reduction=cfg.scenario_require_hole_reduction,
        require_downside_improvement=cfg.scenario_require_downside_improvement,
        require_delta_improvement=cfg.scenario_require_delta_improvement,
        require_expected_pnl_improvement=cfg.scenario_require_expected_pnl_improvement,
    )


def scenario_risk_is_active(cfg: StrategyConfig) -> bool:
    if not cfg.enable_scenario_risk:
        return False

    limits = scenario_limits_from_config(cfg)
    return any(
        value is not None
        for value in (
            limits.max_surface_flatness,
            limits.max_terminal_negative_cells,
            limits.max_payoff_variance,
            limits.min_expected_pnl,
            limits.min_max_loss,
            limits.max_terminal_downside,
            limits.max_terminal_abs_delta,
        )
    ) or any(
        (
            limits.require_flatness_improvement,
            limits.require_variance_improvement,
            limits.require_hole_reduction,
            limits.require_downside_improvement,
            limits.require_delta_improvement,
            limits.require_expected_pnl_improvement,
        )
    )


def contracts_from_positions(positions: Iterable[object]) -> list[ScenarioContract]:
    return [contract_from_position(position) for position in positions]


def quantity_schedule(initial_quantity: int, reduce_size_to_fit: bool) -> tuple[int, ...]:
    if initial_quantity < 1:
        return ()
    if not reduce_size_to_fit:
        return (initial_quantity,)

    step = max(1, math.ceil(initial_quantity / 5))
    quantities: list[int] = []
    current = initial_quantity
    while current > 0:
        quantities.append(current)
        current -= step
    return tuple(quantities)


def reduced_quantity_schedule(initial_quantity: int, reduce_size_to_fit: bool) -> tuple[int, ...]:
    return quantity_schedule(initial_quantity, reduce_size_to_fit)[1:]


def evaluate_candidate_quantity(
    cfg: StrategyConfig,
    current_positions: Iterable[object],
    candidate_position: object,
    initial_quantity: int,
    params: BatesParams,
    as_of: dt.datetime,
    spot_price: float,
    cache: ScenarioEvaluationCache | None = None,
    params_identifier: str | None = None,
) -> ScenarioGateResult:
    if initial_quantity < 1 or not scenario_risk_is_active(cfg):
        return ScenarioGateResult(
            approved_quantity=max(initial_quantity, 0),
            comparison=None,
            decision=None,
        )

    if cache is None:
        cache = ScenarioEvaluationCache()
    if params_identifier is None:
        params_identifier = params.calibrated_at or "params"

    current_contracts = contracts_from_positions(current_positions)
    limits = scenario_limits_from_config(cfg)
    probability_method = "bates" if cfg.scenario_use_bates_probabilities else "lognormal"
    initial_candidate_contract = contract_from_position(
        SimpleNamespace(
            instrument=candidate_position.instrument,
            side=candidate_position.side,
            quantity=initial_quantity,
            entry_price=candidate_position.entry_price,
            event_ticker=getattr(candidate_position, "event_ticker", ""),
        )
    )
    grid = build_scenario_grid(
        as_of=as_of,
        spot_price=spot_price,
        contracts=list(current_contracts) + [initial_candidate_contract],
        price_range_pct=cfg.scenario_price_range_pct,
        price_step=cfg.scenario_price_step,
        time_step_hours=cfg.scenario_time_step_hours,
    )
    current_surface = build_portfolio_surface(current_contracts, params=params, grid=grid)
    probability_key = cache._probability_key(
        params_identifier=params_identifier,
        probability_method=probability_method,
        spot_price=spot_price,
        as_of=as_of,
        grid=grid,
    )
    probability_weights = cache.probability_weights.get(probability_key)
    if probability_key not in cache.probability_weights:
        probability_weights = probability_weights_for_grid(
            params=params,
            spot_price=spot_price,
            grid=grid,
            as_of=as_of,
            probability_method=probability_method,
        )
        cache.probability_weights[probability_key] = probability_weights

    def comparison_for_quantity(quantity: int) -> ScenarioComparison:
        surface_key = cache._candidate_key(
            instrument=candidate_position.instrument,
            side=candidate_position.side,
            entry_price=candidate_position.entry_price,
            quantity=quantity,
            grid=grid,
            params_identifier=params_identifier,
        )
        candidate_addition_surface = cache.candidate_surfaces.get(surface_key)
        if candidate_addition_surface is None:
            candidate_contract = contract_from_position(
                SimpleNamespace(
                    instrument=candidate_position.instrument,
                    side=candidate_position.side,
                    quantity=quantity,
                    entry_price=candidate_position.entry_price,
                    event_ticker=getattr(candidate_position, "event_ticker", ""),
                )
            )
            candidate_addition_surface = build_portfolio_surface(
                [candidate_contract],
                params=params,
                grid=grid,
            )
            cache.candidate_surfaces[surface_key] = candidate_addition_surface
        return compare_surface_addition(
            current_surface=current_surface,
            candidate_addition_surface=candidate_addition_surface,
            probability_weights=probability_weights,
        )

    last_comparison: ScenarioComparison | None = None
    last_decision: ScenarioDecision | None = None

    initial_comparison = comparison_for_quantity(initial_quantity)
    initial_decision = decide_candidate_trade(initial_comparison, limits)
    if initial_decision.accepted:
        return ScenarioGateResult(
            approved_quantity=initial_quantity,
            comparison=initial_comparison,
            decision=initial_decision,
        )
    last_comparison = initial_comparison
    last_decision = initial_decision

    for quantity in reduced_quantity_schedule(
        initial_quantity=initial_quantity,
        reduce_size_to_fit=cfg.scenario_reduce_size_to_fit,
    ):
        comparison = comparison_for_quantity(quantity)
        decision = decide_candidate_trade(comparison, limits)
        if decision.accepted:
            return ScenarioGateResult(
                approved_quantity=quantity,
                comparison=comparison,
                decision=decision,
            )
        last_comparison = comparison
        last_decision = decision

    return ScenarioGateResult(
        approved_quantity=0,
        comparison=last_comparison,
        decision=last_decision,
    )
