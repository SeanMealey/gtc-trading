"""
Unit tests for the live runner.

These tests mock the GeminiExecutionClient and the bates_pricer module so
they run without network access or the C++ extension.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "pricer"))

from strategy.config import StrategyConfig
from strategy.execution import GeminiExecutionClient, OrderResult
from strategy.position_log import Position, PositionLog
from strategy.trade_ledger import TradeLedger
from strategy.runner import LiveRunner, RiskState


def _make_cfg(**overrides) -> StrategyConfig:
    defaults = dict(
        submit_orders=True,
        dry_run=False,
        require_state_reconciliation=False,
        max_params_age_hours=9999,
        poll_interval_seconds=0.01,
        log_heartbeat_every_n_loops=1,
        max_notional_per_order_usd=50.0,
        max_total_notional_usd=500.0,
        max_quantity_per_order=100,
        max_open_positions_live=20,
        max_consecutive_api_failures=3,
        daily_loss_limit_usd=50.0,
        daily_filled_notional_cap_usd=500.0,
        enable_scenario_risk=False,
        enable_inventory_skew=False,
        kill_switch_path="/tmp/gtc-test-ks-never-exists",
        positions_path="",
        trades_log_path="",
        runner_log_path="",
    )
    defaults.update(overrides)
    return StrategyConfig(**defaults)


def _make_params():
    from calibration.params import BatesParams
    return BatesParams(
        S=100000.0, r=0.04, q=0.0,
        v0=0.2025, kappa=2.0, theta=0.16, sigma_v=0.8, rho=-0.55,
        lam=4.0, mu_j=-0.04, sigma_j=0.2,
        calibration_source="test",
        calibrated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


def _mock_events():
    expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12)
    event_code = expiry.strftime("%y%m%d%H%M")
    instrument = f"GEMI-BTC{event_code}-HI100000"
    return [
        {
            "ticker": "EVT-TEST",
            "contracts": [
                {
                    "instrumentSymbol": instrument,
                    "prices": {
                        "bestBid": "0.30",
                        "bestAsk": "0.40",
                        "bestBidSize": "10",
                        "bestAskSize": "10",
                    },
                    "totalShares": 100,
                }
            ],
        }
    ]


def _fill_result(**kw) -> OrderResult:
    defaults = dict(
        order_id="oid-1",
        client_order_id="coid-1",
        instrument="",
        side="buy",
        outcome="yes",
        requested_quantity=5,
        requested_price=0.40,
        filled_quantity=5,
        avg_execution_price=0.40,
        fee=0.01,
        status="filled",
        is_live=False,
        is_cancelled=False,
        created_at_ms=1700000000000,
        raw={},
    )
    defaults.update(kw)
    return OrderResult(**defaults)


class RunnerTickTests(unittest.TestCase):
    def _build_runner(self, cfg, tmpdir) -> tuple[LiveRunner, MagicMock]:
        cfg.positions_path = os.path.join(tmpdir, "positions.json")
        cfg.trades_log_path = os.path.join(tmpdir, "trades.csv")
        cfg.runner_log_path = os.path.join(tmpdir, "runner.log")

        params = _make_params()
        client = MagicMock(spec=GeminiExecutionClient)
        client.get_active_events.return_value = _mock_events()
        client.get_spot.return_value = 100000.0
        client.get_positions.return_value = []
        client.get_active_orders.return_value = []
        client.get_order_history.return_value = []
        _order_status_raw = {
            "order_id": "oid-1",
            "symbol": "",
            "side": "buy",
            "outcome": "yes",
            "original_amount": "5",
            "executed_amount": "5",
            "avg_execution_price": "0.40",
            "fee": "0.01",
            "status": "filled",
            "is_live": False,
            "is_cancelled": False,
        }
        client.get_order_status.return_value = _order_status_raw
        client.place_order.return_value = _fill_result()

        logger = logging.getLogger(f"test-{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        position_log = PositionLog(cfg.positions_path)
        ledger = TradeLedger(cfg.trades_log_path)

        runner = LiveRunner(
            cfg=cfg, client=client, params=params,
            position_log=position_log, ledger=ledger, logger=logger,
        )
        return runner, client

    @patch("strategy.runner.bp")
    def test_tick_submits_order_on_positive_edge(self, mock_bp):
        mock_bp.binary_call.return_value = 0.55
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_cfg(min_edge=0.03, flat_amount_usd=5.0, require_two_sided=True)
            runner, client = self._build_runner(cfg, tmpdir)

            runner.run(max_loops=1)

            self.assertTrue(client.place_order.called)
            call_kwargs = client.place_order.call_args
            self.assertEqual(call_kwargs.kwargs["side"], "buy")

    @patch("strategy.runner.bp")
    def test_tick_skips_when_edge_too_small(self, mock_bp):
        mock_bp.binary_call.return_value = 0.41
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_cfg(min_edge=0.10)
            runner, client = self._build_runner(cfg, tmpdir)

            runner.run(max_loops=1)

            client.place_order.assert_not_called()

    @patch("strategy.runner.bp")
    def test_tick_records_fill_in_position_log(self, mock_bp):
        mock_bp.binary_call.return_value = 0.55
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_cfg(min_edge=0.03)
            runner, client = self._build_runner(cfg, tmpdir)

            runner.run(max_loops=1)

            positions = runner.position_log.open_positions()
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].side, "buy")
            self.assertEqual(positions[0].quantity, 5)

    @patch("strategy.runner.bp")
    def test_tick_records_trade_in_ledger(self, mock_bp):
        mock_bp.binary_call.return_value = 0.55
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_cfg(min_edge=0.03)
            runner, client = self._build_runner(cfg, tmpdir)

            runner.run(max_loops=1)

            rows = runner.ledger.read_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "buy")
            self.assertEqual(rows[0]["filled_quantity"], "5")

    @patch("strategy.runner.bp")
    def test_dry_mode_logs_but_does_not_submit(self, mock_bp):
        mock_bp.binary_call.return_value = 0.55
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_cfg(min_edge=0.03, submit_orders=False)
            runner, client = self._build_runner(cfg, tmpdir)

            runner.run(max_loops=1)

            client.place_order.assert_not_called()
            rows = runner.ledger.read_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "dry")

    @patch("strategy.runner.bp")
    def test_kill_switch_stops_runner(self, mock_bp):
        mock_bp.binary_call.return_value = 0.55
        with tempfile.TemporaryDirectory() as tmpdir:
            ks_path = os.path.join(tmpdir, "KILL")
            with open(ks_path, "w") as f:
                f.write("kill")

            cfg = _make_cfg(kill_switch_path=ks_path)
            runner, client = self._build_runner(cfg, tmpdir)

            runner.run(max_loops=5)

            client.place_order.assert_not_called()
            self.assertTrue(runner._stop)

    @patch("strategy.runner.bp")
    def test_circuit_opens_after_api_failures(self, mock_bp):
        mock_bp.binary_call.return_value = 0.55
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_cfg(max_consecutive_api_failures=2)
            runner, client = self._build_runner(cfg, tmpdir)

            from strategy.execution import ExecutionError
            client.get_active_events.side_effect = ExecutionError("boom")

            runner.run(max_loops=5)

            self.assertTrue(runner.risk.circuit_open)
            client.place_order.assert_not_called()

    @patch("strategy.runner.bp")
    def test_max_notional_cap_scales_quantity(self, mock_bp):
        mock_bp.binary_call.return_value = 0.55
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_cfg(
                min_edge=0.03,
                flat_amount_usd=100.0,
                max_notional_per_order_usd=2.0,
            )
            runner, client = self._build_runner(cfg, tmpdir)
            client.place_order.return_value = _fill_result(
                filled_quantity=5, requested_quantity=5,
            )

            runner.run(max_loops=1)

            if client.place_order.called:
                submitted_qty = client.place_order.call_args.kwargs["quantity"]
                self.assertLessEqual(submitted_qty * 0.40, 2.0 + 0.001)


class RiskStateTests(unittest.TestCase):
    def test_day_reset(self) -> None:
        risk = RiskState()
        risk.daily_filled_notional_usd = 100.0
        risk.daily_realised_pnl_usd = -10.0
        risk.day_marker = "2026-04-10"

        tomorrow = dt.datetime(2026, 4, 11, 12, 0, tzinfo=dt.timezone.utc)
        risk.reset_day_if_needed(tomorrow)

        self.assertAlmostEqual(risk.daily_filled_notional_usd, 0.0)
        self.assertAlmostEqual(risk.daily_realised_pnl_usd, 0.0)
        self.assertEqual(risk.day_marker, "2026-04-11")


if __name__ == "__main__":
    unittest.main()
