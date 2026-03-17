from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "pricer"))

import bates_pricer as bp
from calibration.params import BatesParams
from strategy.config import StrategyConfig
from strategy.scenario_matrix import (
    ScenarioContract,
    ScenarioGrid,
    ScenarioRiskLimits,
    ScenarioSurface,
    bates_implied_price_probabilities,
    build_portfolio_surface,
    build_scenario_grid,
    compare_candidate_trade,
    compute_surface_metrics,
    decide_candidate_trade,
    lognormal_price_probabilities,
)
from strategy.scenario_risk import (
    ScenarioEvaluationCache,
    evaluate_candidate_quantity,
    quantity_schedule,
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
        self.assertAlmostEqual(metrics.terminal_downside, 0.5)
        self.assertGreater(metrics.terminal_max_abs_delta, 0.0)

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
        self.assertFalse(comparison.expected_pnl_improved)

        ev_decision = decide_candidate_trade(
            comparison,
            ScenarioRiskLimits(require_expected_pnl_improvement=True),
        )
        self.assertFalse(ev_decision.accepted)
        self.assertIn("candidate trade does not improve expected pnl", ev_decision.reasons)

    def test_candidate_quantity_is_reduced_to_fit_scenario_limits(self) -> None:
        as_of = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.timezone.utc)
        cfg = StrategyConfig(
            enable_scenario_risk=True,
            scenario_reduce_size_to_fit=True,
            scenario_use_bates_probabilities=False,
            scenario_price_range_pct=0.10,
            scenario_price_step=10000.0,
            scenario_time_step_hours=4.0,
            scenario_max_terminal_downside=0.7,
        )

        candidate = type(
            "Candidate",
            (),
            {
                "instrument": "GEMI-BTC2603141100-HI100000",
                "side": "buy",
                "quantity": 2,
                "entry_price": 0.6,
                "event_ticker": "BTC2603141100",
            },
        )()

        gate = evaluate_candidate_quantity(
            cfg=cfg,
            current_positions=[],
            candidate_position=candidate,
            initial_quantity=2,
            params=self.params,
            as_of=as_of,
            spot_price=100000.0,
        )

        self.assertEqual(gate.approved_quantity, 1)
        self.assertIsNotNone(gate.comparison)
        self.assertTrue(gate.decision.accepted)
        self.assertAlmostEqual(gate.comparison.candidate_metrics.terminal_downside, 0.6)

    def test_quantity_schedule_checks_fifths_of_original_size(self) -> None:
        self.assertEqual(quantity_schedule(100, True), (100, 80, 60, 40, 20))
        self.assertEqual(quantity_schedule(9, True), (9, 7, 5, 3, 1))
        self.assertEqual(quantity_schedule(100, False), (100,))

    def test_reduced_sizes_are_only_checked_after_initial_denial(self) -> None:
        as_of = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.timezone.utc)
        cfg = StrategyConfig(
            enable_scenario_risk=True,
            scenario_reduce_size_to_fit=True,
            scenario_use_bates_probabilities=False,
            scenario_price_range_pct=0.10,
            scenario_price_step=10000.0,
            scenario_time_step_hours=4.0,
            scenario_max_terminal_downside=2.0,
        )
        cache = ScenarioEvaluationCache()

        candidate = type(
            "Candidate",
            (),
            {
                "instrument": "GEMI-BTC2603141100-HI100000",
                "side": "buy",
                "quantity": 100,
                "entry_price": 0.01,
                "event_ticker": "BTC2603141100",
            },
        )()

        gate = evaluate_candidate_quantity(
            cfg=cfg,
            current_positions=[],
            candidate_position=candidate,
            initial_quantity=100,
            params=self.params,
            as_of=as_of,
            spot_price=100000.0,
            cache=cache,
            params_identifier="test-params",
        )

        self.assertEqual(gate.approved_quantity, 100)
        self.assertEqual(len(cache.candidate_surfaces), 1)

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

    def test_binary_call_batch_matches_scalar_pricer(self) -> None:
        pricer_params = self.params.to_pricer_params()
        spots = np.asarray([95000.0, 97500.0, 100000.0, 102500.0, 105000.0], dtype=float)

        batch_prices = np.asarray(
            bp.binary_call_batch(100000.0, 1.0 / 365.25, pricer_params, spots, N=128),
            dtype=float,
        )
        scalar_prices = np.asarray(
            [bp.binary_call(100000.0, 1.0 / 365.25, _params_with_spot(pricer_params, spot), N=128) for spot in spots],
            dtype=float,
        )

        np.testing.assert_allclose(batch_prices, scalar_prices, rtol=1e-10, atol=1e-10)

    def test_bates_implied_price_probabilities_sum_to_one(self) -> None:
        prices = np.asarray([95000.0, 97500.0, 100000.0, 102500.0, 105000.0], dtype=float)
        probabilities = bates_implied_price_probabilities(
            params=self.params,
            spot_price=100000.0,
            prices=prices,
            horizon_years=1.0 / 365.25,
            N=128,
        )

        self.assertEqual(len(probabilities), len(prices))
        self.assertTrue(np.all(probabilities >= 0.0))
        np.testing.assert_allclose(probabilities.sum(), 1.0, rtol=0.0, atol=1e-12)

    def test_bates_implied_price_probabilities_match_tail_differences(self) -> None:
        prices = np.asarray([95000.0, 97500.0, 100000.0, 102500.0, 105000.0], dtype=float)
        probabilities = bates_implied_price_probabilities(
            params=self.params,
            spot_price=100000.0,
            prices=prices,
            horizon_years=1.0 / 365.25,
            N=128,
        )
        mids = (prices[:-1] + prices[1:]) / 2.0
        pricer_params = self.params.to_pricer_params()
        pricer_params.S = 100000.0
        tails = np.asarray(
            [bp.binary_call_prob(float(strike), 1.0 / 365.25, pricer_params, N=128) for strike in mids],
            dtype=float,
        )
        tails = np.minimum.accumulate(np.clip(tails, 0.0, 1.0))

        self.assertAlmostEqual(probabilities[0], 1.0 - tails[0], places=12)
        np.testing.assert_allclose(probabilities[1:-1], tails[:-1] - tails[1:], rtol=0.0, atol=1e-12)
        self.assertAlmostEqual(probabilities[-1], tails[-1], places=12)


def _params_with_spot(pricer_params, spot: float):
    pricer_params.S = float(spot)
    return pricer_params


if __name__ == "__main__":
    unittest.main()
