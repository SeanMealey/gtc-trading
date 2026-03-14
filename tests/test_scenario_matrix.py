from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from calibration.params import BatesParams
from strategy.scenario_matrix import (
    ScenarioContract,
    ScenarioGrid,
    ScenarioRiskLimits,
    ScenarioSurface,
    build_portfolio_surface,
    build_scenario_grid,
    compare_candidate_trade,
    compute_surface_metrics,
    decide_candidate_trade,
    lognormal_price_probabilities,
)


class ScenarioMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.params = BatesParams(
            S=100000.0,
            r=0.04,
            q=0.0,
            v0=0.45 ** 2,
            kappa=2.0,
            theta=0.40 ** 2,
            sigma_v=0.80,
            rho=-0.55,
            lam=4.0,
            mu_j=-0.04,
            sigma_j=0.20,
        )

    def test_build_scenario_grid_covers_price_and_time_ranges(self) -> None:
        as_of = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.timezone.utc)
        contract = ScenarioContract(
            instrument="GEMI-BTC2603151200-HI100000",
            side="buy",
            quantity=1,
            entry_price=0.5,
            strike=100000.0,
            expiry_dt=dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.timezone.utc),
        )

        grid = build_scenario_grid(
            as_of=as_of,
            spot_price=100000.0,
            contracts=[contract],
            price_range_pct=0.10,
            price_step=5000.0,
            time_step_hours=12.0,
            horizon_dt=dt.datetime(2026, 3, 16, 12, 0, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(grid.prices[0], 90000.0)
        self.assertEqual(grid.prices[-1], 110000.0)
        self.assertEqual(len(grid.evaluation_times), 5)

    def test_build_portfolio_surface_handles_settled_contracts(self) -> None:
        as_of = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.timezone.utc)
        grid = ScenarioGrid(
            prices=np.asarray([95000.0, 100000.0, 105000.0]),
            evaluation_times=(as_of, as_of + dt.timedelta(hours=4)),
        )
        contract = ScenarioContract(
            instrument="GEMI-BTC2603131200-HI100000",
            side="buy",
            quantity=2,
            entry_price=0.4,
            strike=100000.0,
            expiry_dt=as_of - dt.timedelta(hours=1),
        )

        surface = build_portfolio_surface([contract], params=self.params, grid=grid)

        np.testing.assert_allclose(
            surface.pnl,
            np.asarray(
                [
                    [-0.8, 1.2, 1.2],
                    [-0.8, 1.2, 1.2],
                ]
            ),
        )

    def test_compute_surface_metrics_detects_terminal_holes(self) -> None:
        as_of = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.timezone.utc)
        surface = ScenarioSurface(
            grid=ScenarioGrid(
                prices=np.asarray([95000.0, 97500.0, 100000.0, 102500.0, 105000.0]),
                evaluation_times=(as_of, as_of + dt.timedelta(hours=4)),
            ),
            pnl=np.asarray(
                [
                    [0.2, -0.1, -0.4, 0.1, 0.3],
                    [0.1, -0.2, -0.3, 0.2, 0.4],
                ]
            ),
        )

        metrics = compute_surface_metrics(surface)

        self.assertEqual(metrics.terminal_negative_cells, 2)
        self.assertEqual(len(metrics.hole_ranges), 1)
        self.assertEqual(metrics.hole_ranges[0].start_price, 97500.0)
        self.assertEqual(metrics.hole_ranges[0].end_price, 100000.0)
        self.assertAlmostEqual(metrics.max_loss, -0.4)

    def test_candidate_comparison_and_decision(self) -> None:
        as_of = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.timezone.utc)
        horizon_dt = as_of + dt.timedelta(hours=8)
        current_contract = ScenarioContract(
            instrument="GEMI-BTC2603141100-HI100000",
            side="buy",
            quantity=1,
            entry_price=0.6,
            strike=100000.0,
            expiry_dt=as_of - dt.timedelta(hours=1),
        )
        candidate_contract = ScenarioContract(
            instrument="GEMI-BTC2603141100-HI100000",
            side="sell",
            quantity=1,
            entry_price=0.6,
            strike=100000.0,
            expiry_dt=as_of - dt.timedelta(hours=1),
        )
        probabilities = lognormal_price_probabilities(
            spot_price=100000.0,
            prices=np.asarray([90000.0, 100000.0, 110000.0]),
            horizon_years=1.0 / 365.25,
            sigma=0.4,
            risk_free_rate=0.0,
        )

        comparison = compare_candidate_trade(
            current_contracts=[current_contract],
            candidate_contract=candidate_contract,
            params=self.params,
            as_of=as_of,
            spot_price=100000.0,
            price_range_pct=0.10,
            price_step=10000.0,
            time_step_hours=4.0,
            horizon_dt=horizon_dt,
            probability_weights=probabilities,
        )

        self.assertTrue(comparison.flatness_improved)
        self.assertTrue(comparison.variance_improved)
        self.assertTrue(comparison.max_loss_improved)

        decision = decide_candidate_trade(
            comparison,
            ScenarioRiskLimits(
                require_flatness_improvement=True,
                require_variance_improvement=True,
                min_max_loss=-0.01,
            ),
        )
        self.assertTrue(decision.accepted)

    def test_lognormal_probabilities_fallback_when_grid_misses_mass(self) -> None:
        probabilities = lognormal_price_probabilities(
            spot_price=90000.0,
            prices=np.asarray([69000.0, 70000.0, 71000.0]),
            horizon_years=1.0 / (365.25 * 24.0),
            sigma=0.10,
            risk_free_rate=0.0,
        )

        np.testing.assert_allclose(probabilities.sum(), 1.0)
        self.assertEqual(int(np.argmax(probabilities)), 2)


if __name__ == "__main__":
    unittest.main()
