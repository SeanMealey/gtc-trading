from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from strategy.config import StrategyConfig
from strategy.inventory_skew import evaluate_inventory_skew
from strategy.scenario_matrix import (
    ScenarioGrid,
    ScenarioSurface,
    compare_surface_addition,
    compute_surface_metrics,
)


class InventorySkewTests(unittest.TestCase):
    def _grid(self) -> ScenarioGrid:
        as_of = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.timezone.utc)
        return ScenarioGrid(
            prices=np.asarray([95000.0, 97500.0, 100000.0, 102500.0, 105000.0]),
            evaluation_times=(as_of, as_of + dt.timedelta(hours=4)),
        )

    def test_compute_surface_metrics_reports_pin_risk_near_strike(self) -> None:
        grid = self._grid()
        surface = ScenarioSurface(
            grid=grid,
            pnl=np.asarray(
                [
                    [-0.20, -0.10, 0.10, 0.20, 0.25],
                    [-0.30, -0.20, 0.80, 0.90, 0.95],
                ],
                dtype=float,
            ),
            contract_strikes=(100000.0,),
        )

        metrics = compute_surface_metrics(surface, pin_risk_window_steps=1)

        self.assertAlmostEqual(metrics.terminal_pin_risk, 1.1, places=12)

    def test_inventory_skew_rewards_trade_that_reduces_pin_risk_and_flatness(self) -> None:
        grid = self._grid()
        current_surface = ScenarioSurface(
            grid=grid,
            pnl=np.asarray(
                [
                    [-0.20, -0.10, 0.10, 0.20, 0.25],
                    [-0.30, -0.20, 0.80, 0.90, 0.95],
                ],
                dtype=float,
            ),
            contract_strikes=(100000.0,),
        )
        candidate_addition_surface = ScenarioSurface(
            grid=grid,
            pnl=np.asarray(
                [
                    [0.05, 0.04, -0.04, -0.05, -0.06],
                    [0.15, 0.14, -0.64, -0.70, -0.74],
                ],
                dtype=float,
            ),
            contract_strikes=(100000.0,),
        )
        comparison = compare_surface_addition(
            current_surface=current_surface,
            candidate_addition_surface=candidate_addition_surface,
            pin_risk_window_steps=1,
        )
        cfg = StrategyConfig(
            enable_inventory_skew=True,
            inventory_skew_flatness_weight=1.0,
            inventory_skew_max_loss_weight=1.0,
            inventory_skew_pin_risk_weight=1.0,
            inventory_skew_max_edge_credit=0.05,
            inventory_skew_max_edge_penalty=0.05,
        )

        decision = evaluate_inventory_skew(
            cfg=cfg,
            raw_edge=0.025,
            base_required_edge=0.03,
            comparison=comparison,
            requested_quantity=10,
        )

        self.assertGreater(decision.score, 0.0)
        self.assertGreater(decision.inventory_adjustment, 0.0)
        self.assertLess(decision.effective_required_edge, 0.03)
        self.assertTrue(decision.passes_inventory_filter)
        self.assertGreaterEqual(decision.adjusted_quantity, 10)
        self.assertGreater(decision.delta_pin_risk, 0.0)

    def test_inventory_skew_penalizes_trade_that_worsens_risk_profile(self) -> None:
        grid = self._grid()
        current_surface = ScenarioSurface(
            grid=grid,
            pnl=np.asarray(
                [
                    [0.10, 0.12, 0.15, 0.16, 0.18],
                    [0.08, 0.10, 0.14, 0.16, 0.17],
                ],
                dtype=float,
            ),
            contract_strikes=(100000.0,),
        )
        candidate_addition_surface = ScenarioSurface(
            grid=grid,
            pnl=np.asarray(
                [
                    [-0.15, -0.10, 0.00, 0.10, 0.15],
                    [-0.70, -0.60, 0.20, 0.85, 0.95],
                ],
                dtype=float,
            ),
            contract_strikes=(100000.0,),
        )
        comparison = compare_surface_addition(
            current_surface=current_surface,
            candidate_addition_surface=candidate_addition_surface,
            pin_risk_window_steps=1,
        )
        cfg = StrategyConfig(
            enable_inventory_skew=True,
            inventory_skew_flatness_weight=1.0,
            inventory_skew_max_loss_weight=1.0,
            inventory_skew_pin_risk_weight=1.0,
            inventory_skew_require_positive_score=True,
            inventory_skew_max_edge_credit=0.05,
            inventory_skew_max_edge_penalty=0.05,
            inventory_skew_size_multiplier_min=0.5,
        )

        decision = evaluate_inventory_skew(
            cfg=cfg,
            raw_edge=0.03,
            base_required_edge=0.03,
            comparison=comparison,
            requested_quantity=10,
        )

        self.assertLess(decision.score, 0.0)
        self.assertGreater(decision.effective_required_edge, 0.03)
        self.assertFalse(decision.passes_inventory_filter)
        self.assertLess(decision.adjusted_quantity, 10)
        self.assertLess(decision.delta_pin_risk, 0.0)


if __name__ == "__main__":
    unittest.main()
