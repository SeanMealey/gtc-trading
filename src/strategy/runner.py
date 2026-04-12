"""
Live market-making runner for Gemini BTC binary options.

The runner is intentionally a single, linear loop:

  1. Pre-loop: load Bates params, load local position state, reconcile against
     Gemini truth, refuse to start if reconciliation fails.
  2. Each tick:
       a. honour kill switch + circuit breaker
       b. fetch active BTC events + spot price
       c. for each contract:
            - parse strike / expiry from instrument symbol
            - price with the C++ COS pricer
            - run signal logic
            - run inventory-skew + scenario gates
            - size with sizing module
            - apply live-only safety caps
            - submit Fast API order via ExecutionClient
            - record actual fill in trade ledger and position log
       d. emit a heartbeat line every N loops

The runner records the order state returned by Gemini and any cached
`orders@account` websocket event observed during placement.

Run with `python -m strategy.runner --config config/live.json` from `src/`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Iterable

# Allow `python src/strategy/runner.py` and `python -m strategy.runner`.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
sys.path.insert(0, os.path.join(_SRC_DIR, "pricer"))

from calibration.params import BatesParams  # noqa: E402
from strategy.config import StrategyConfig  # noqa: E402
from strategy.execution import ExecutionError, GeminiExecutionClient, OrderResult  # noqa: E402
from strategy.inventory_skew import (  # noqa: E402
    evaluate_inventory_skew,
    inventory_skew_is_active,
)
from strategy.position_log import Position, PositionLog  # noqa: E402
from strategy.scenario_matrix import (  # noqa: E402
    ScenarioComparison,
    ScenarioContract,
    build_portfolio_surface,
    build_scenario_grid,
    compare_surface_addition,
    contract_from_position,
    parse_instrument,
)
from strategy.scenario_risk import (  # noqa: E402
    ScenarioEvaluationCache,
    contracts_from_positions,
    evaluate_candidate_quantity,
    scenario_risk_is_active,
)
from strategy.signal import Signal, generate_signal  # noqa: E402
from strategy.sizing import flat_size, kelly_size  # noqa: E402
from strategy.trade_ledger import LedgerRow, TradeLedger  # noqa: E402

import bates_pricer as bp  # noqa: E402


SECONDS_PER_YEAR = 365.25 * 24 * 3600


# ── data structures ────────────────────────────────────────────────────────


@dataclass
class ContractView:
    instrument: str
    event_ticker: str
    strike: float
    expiry_dt: dt.datetime
    bid: float | None
    ask: float | None
    bid_size: float
    ask_size: float
    total_shares: float


@dataclass
class RiskState:
    consecutive_api_failures: int = 0
    daily_filled_notional_usd: float = 0.0
    daily_realised_pnl_usd: float = 0.0
    day_marker: str = ""
    circuit_open: bool = False
    circuit_reason: str = ""

    def reset_day_if_needed(self, now: dt.datetime) -> None:
        marker = now.strftime("%Y-%m-%d")
        if self.day_marker != marker:
            self.day_marker = marker
            self.daily_filled_notional_usd = 0.0
            self.daily_realised_pnl_usd = 0.0


@dataclass
class RunnerStats:
    loops: int = 0
    quotes_evaluated: int = 0
    signals: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    last_heartbeat: str = ""


# ── helpers ────────────────────────────────────────────────────────────────


def _setup_logging(path: str) -> logging.Logger:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    logger = logging.getLogger("live_runner")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(path)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_iso(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _params_age_hours(params: BatesParams, now: dt.datetime) -> float | None:
    parsed = _parse_iso(params.calibrated_at)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (now - parsed).total_seconds() / 3600.0


def _t_years(expiry_dt: dt.datetime, now: dt.datetime) -> float:
    return max((expiry_dt - now).total_seconds(), 0.0) / SECONDS_PER_YEAR


def _outcome_for_side(side: str) -> str:
    # All Gemini BTC binary instruments use the YES contract; we express direction via side.
    return "yes"


def _make_client_order_id() -> str:
    return f"mm-{uuid.uuid4().hex[:12]}"


def _initial_calibration_due_at(
    params: BatesParams,
    interval_hours: float,
    now: dt.datetime | None = None,
) -> dt.datetime:
    current_time = now or _now_utc()
    if interval_hours <= 0:
        return current_time + dt.timedelta(days=3650)

    calibrated_at = _parse_iso(params.calibrated_at or "")
    if calibrated_at is None:
        return current_time
    if calibrated_at.tzinfo is None:
        calibrated_at = calibrated_at.replace(tzinfo=dt.timezone.utc)
    return calibrated_at + dt.timedelta(hours=interval_hours)


# ── runner ─────────────────────────────────────────────────────────────────


class LiveRunner:
    def __init__(
        self,
        cfg: StrategyConfig,
        client: GeminiExecutionClient,
        params: BatesParams,
        position_log: PositionLog,
        ledger: TradeLedger,
        logger: logging.Logger,
    ):
        self.cfg = cfg
        self.client = client
        self.params = params
        self.position_log = position_log
        self.ledger = ledger
        self.logger = logger
        self.risk = RiskState()
        self.stats = RunnerStats()
        self._stop = False
        self._next_calibration_due_at = _initial_calibration_due_at(
            params,
            self.cfg.calibration_interval_hours,
        )

    # ── lifecycle ──────────────────────────────────────────────────────────

    def request_stop(self, *_args) -> None:
        self.logger.info("stop requested")
        self._stop = True

    def run(self, *, max_loops: int | None = None) -> None:
        self._install_signal_handlers()
        self._maybe_refresh_deribit_and_params()
        self._preflight()

        loop_count = 0
        while not self._stop:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("tick failed: %s\n%s", exc, traceback.format_exc())
                self._record_api_failure(str(exc))

            loop_count += 1
            if max_loops is not None and loop_count >= max_loops:
                break
            if self._stop:
                break
            time.sleep(self.cfg.poll_interval_seconds)

        self.logger.info(
            "runner stopped after %d loops (orders submitted=%d, filled=%d)",
            loop_count,
            self.stats.orders_submitted,
            self.stats.orders_filled,
        )

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):
                pass

    # ── preflight ──────────────────────────────────────────────────────────

    def _preflight(self) -> None:
        self.logger.info(
            "preflight: base=%s fast_api=%s submit_orders=%s dry_run=%s params_source=%s @ %s",
            self.cfg.gemini_base_url,
            self.cfg.gemini_fast_api_url or "disabled",
            self.cfg.submit_orders,
            self.cfg.dry_run,
            self.params.calibration_source,
            self.params.calibrated_at,
        )

        age = _params_age_hours(self.params, _now_utc())
        if age is None:
            self.logger.warning("params have no calibrated_at timestamp")
        elif age > self.cfg.max_params_age_hours:
            raise RuntimeError(
                f"params are stale: {age:.1f}h old "
                f"(max {self.cfg.max_params_age_hours:.1f}h)"
            )
        else:
            self.logger.info("params age: %.1fh", age)

        if self.cfg.require_state_reconciliation:
            self._reconcile_state()
        else:
            self.logger.warning("state reconciliation disabled")

    def _reconcile_state(self) -> None:
        local_positions = self.position_log.open_positions()
        try:
            remote_positions = self.client.get_positions()
            remote_active = self.client.get_active_orders()
        except ExecutionError as exc:
            raise RuntimeError(f"reconciliation failed fetching exchange state: {exc}") from exc

        local_by_instrument: dict[str, int] = {}
        for pos in local_positions:
            qty = pos.quantity if pos.side == "buy" else -pos.quantity
            local_by_instrument[pos.instrument] = local_by_instrument.get(pos.instrument, 0) + qty

        remote_by_instrument: dict[str, int] = {}
        for raw in remote_positions:
            instrument = str(
                raw.get("symbol")
                or raw.get("instrumentSymbol")
                or raw.get("instrument")
                or ""
            )
            if not instrument:
                continue
            try:
                qty = int(float(raw.get("netQuantity") or raw.get("quantity") or 0))
            except (TypeError, ValueError):
                qty = 0
            if str(raw.get("side", "")).lower() == "sell":
                qty = -abs(qty)
            remote_by_instrument[instrument] = remote_by_instrument.get(instrument, 0) + qty

        keys = set(local_by_instrument) | set(remote_by_instrument)
        drifts = []
        for key in sorted(keys):
            diff = abs(
                local_by_instrument.get(key, 0) - remote_by_instrument.get(key, 0)
            )
            if diff > self.cfg.reconciliation_max_quantity_drift:
                drifts.append(
                    (key, local_by_instrument.get(key, 0), remote_by_instrument.get(key, 0))
                )

        if drifts:
            for key, local, remote in drifts:
                self.logger.error(
                    "reconciliation drift: %s local=%d remote=%d", key, local, remote
                )
            raise RuntimeError(
                f"reconciliation found {len(drifts)} drifted instruments — "
                "fix local state before starting"
            )

        self.logger.info(
            "reconciliation OK: %d local positions, %d remote positions, %d active orders",
            len(local_positions),
            len(remote_positions),
            len(remote_active),
        )

    # ── safety ─────────────────────────────────────────────────────────────

    def _kill_switch_engaged(self) -> bool:
        return os.path.exists(self.cfg.kill_switch_path)

    def _check_circuit(self) -> bool:
        if self.risk.circuit_open:
            self.logger.warning("circuit open: %s", self.risk.circuit_reason)
            return False
        if self.risk.consecutive_api_failures >= self.cfg.max_consecutive_api_failures:
            self._open_circuit(
                f"{self.risk.consecutive_api_failures} consecutive API failures"
            )
            return False
        if self.risk.daily_realised_pnl_usd <= -abs(self.cfg.daily_loss_limit_usd):
            self._open_circuit(
                f"daily loss {self.risk.daily_realised_pnl_usd:.2f} USD breached limit"
            )
            return False
        if (
            self.risk.daily_filled_notional_usd
            >= self.cfg.daily_filled_notional_cap_usd
        ):
            self._open_circuit(
                f"daily filled notional cap {self.cfg.daily_filled_notional_cap_usd:.2f} reached"
            )
            return False
        return True

    def _open_circuit(self, reason: str) -> None:
        self.risk.circuit_open = True
        self.risk.circuit_reason = reason
        self.logger.error("CIRCUIT OPEN: %s", reason)

    def _record_api_failure(self, reason: str) -> None:
        self.risk.consecutive_api_failures += 1
        self.logger.warning(
            "api failure (%d/%d): %s",
            self.risk.consecutive_api_failures,
            self.cfg.max_consecutive_api_failures,
            reason,
        )

    def _record_api_success(self) -> None:
        self.risk.consecutive_api_failures = 0

    # ── main tick ──────────────────────────────────────────────────────────

    def _tick(self) -> None:
        self.stats.loops += 1
        now = _now_utc()
        self.risk.reset_day_if_needed(now)

        if self._kill_switch_engaged():
            self.logger.warning("kill switch present at %s — halting", self.cfg.kill_switch_path)
            self._stop = True
            return

        if not self._check_circuit():
            return

        self._maybe_refresh_deribit_and_params(now=now)

        try:
            events = self.client.get_active_events()
            spot = self.client.get_spot()
            self._record_api_success()
        except ExecutionError as exc:
            self._record_api_failure(str(exc))
            return

        if spot is None or spot <= 0:
            self.logger.warning("missing spot price — skipping tick")
            return

        contracts = self._collect_contracts(events, now)
        if not contracts:
            self._heartbeat(now, contracts=0)
            return

        positions = self.position_log.load()
        open_positions = [p for p in positions if p.status == "open"]

        scenario_cache = ScenarioEvaluationCache()
        params_snapshot = BatesParams(**{**self.params.__dict__})
        params_snapshot.S = float(spot)

        for view in contracts:
            self.stats.quotes_evaluated += 1
            try:
                self._evaluate_and_maybe_trade(
                    view=view,
                    spot=spot,
                    params=params_snapshot,
                    open_positions=open_positions,
                    now=now,
                    scenario_cache=scenario_cache,
                )
            except ExecutionError as exc:
                self._record_api_failure(str(exc))
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    "evaluation error for %s: %s\n%s",
                    view.instrument,
                    exc,
                    traceback.format_exc(),
                )

            if not self._check_circuit() or self._stop:
                break

        self._heartbeat(now, contracts=len(contracts))

    def _heartbeat(self, now: dt.datetime, *, contracts: int) -> None:
        if self.cfg.log_heartbeat_every_n_loops <= 0:
            return
        if self.stats.loops % self.cfg.log_heartbeat_every_n_loops != 0:
            return
        self.stats.last_heartbeat = now.isoformat()
        open_positions = self.position_log.open_positions()
        self.logger.info(
            "heartbeat loop=%d contracts=%d open_positions=%d submitted=%d filled=%d "
            "daily_filled_notional=%.2f circuit=%s",
            self.stats.loops,
            contracts,
            len(open_positions),
            self.stats.orders_submitted,
            self.stats.orders_filled,
            self.risk.daily_filled_notional_usd,
            "open" if self.risk.circuit_open else "ok",
        )

    def _maybe_refresh_deribit_and_params(
        self,
        *,
        now: dt.datetime | None = None,
        force: bool = False,
    ) -> None:
        interval_hours = float(self.cfg.calibration_interval_hours)
        if interval_hours <= 0:
            return

        current_time = now or _now_utc()
        if not force and current_time < self._next_calibration_due_at:
            return

        interval = dt.timedelta(hours=interval_hours)
        self._next_calibration_due_at = current_time + interval

        self.logger.info(
            "refreshing Deribit chain + implied params (interval=%.4fh)",
            interval_hours,
        )
        started = time.perf_counter()
        try:
            new_params = _refresh_deribit_and_calibrate(self.cfg, self.logger)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "Deribit refresh/calibration failed; keeping prior params: %s\n%s",
                exc,
                traceback.format_exc(),
            )
            return

        self.params = new_params
        age = _params_age_hours(self.params, _now_utc())
        age_str = "unknown" if age is None else f"{age:.2f}h"
        self.logger.info(
            "updated live params from %s @ %s in %.1fs (age=%s)",
            self.params.calibration_source,
            self.params.calibrated_at,
            time.perf_counter() - started,
            age_str,
        )

    # ── contract collection ────────────────────────────────────────────────

    def _collect_contracts(self, events: Iterable[dict], now: dt.datetime) -> list[ContractView]:
        out: list[ContractView] = []
        for event in events:
            event_ticker = str(event.get("ticker", ""))
            for contract in event.get("contracts", []):
                instrument = str(contract.get("instrumentSymbol", ""))
                parsed = parse_instrument(instrument)
                if parsed is None:
                    continue
                _, expiry_dt, strike = parsed
                if expiry_dt <= now:
                    continue

                prices = contract.get("prices", {}) or {}
                bid_raw = prices.get("bestBid")
                ask_raw = prices.get("bestAsk")
                try:
                    bid = float(bid_raw) if bid_raw not in (None, "") else None
                except (TypeError, ValueError):
                    bid = None
                try:
                    ask = float(ask_raw) if ask_raw not in (None, "") else None
                except (TypeError, ValueError):
                    ask = None
                try:
                    total_shares = float(contract.get("totalShares") or 0)
                except (TypeError, ValueError):
                    total_shares = 0.0

                out.append(
                    ContractView(
                        instrument=instrument,
                        event_ticker=event_ticker,
                        strike=strike,
                        expiry_dt=expiry_dt,
                        bid=bid,
                        ask=ask,
                        bid_size=float(prices.get("bestBidSize") or 0),
                        ask_size=float(prices.get("bestAskSize") or 0),
                        total_shares=total_shares,
                    )
                )
        return out

    # ── per-contract evaluation ────────────────────────────────────────────

    def _evaluate_and_maybe_trade(
        self,
        *,
        view: ContractView,
        spot: float,
        params: BatesParams,
        open_positions: list[Position],
        now: dt.datetime,
        scenario_cache: ScenarioEvaluationCache,
    ) -> None:
        cfg = self.cfg

        if cfg.require_two_sided and (view.bid is None or view.ask is None):
            return
        if view.total_shares < cfg.min_total_shares:
            return
        if view.bid is not None and view.ask is not None:
            spread = view.ask - view.bid
            if spread < 0:
                self.logger.warning("crossed book on %s: bid=%s ask=%s", view.instrument, view.bid, view.ask)
                return
            if spread > cfg.max_book_spread:
                return

        T = _t_years(view.expiry_dt, now)
        if T <= 0:
            return

        try:
            pricer_params = params.to_pricer_params()
            pricer_params.S = float(spot)
            model_price = float(bp.binary_call(view.strike, T, pricer_params, N=256))
        except Exception as exc:  # noqa: BLE001
            self.logger.error("pricer failure on %s: %s", view.instrument, exc)
            return

        sig = generate_signal(
            instrument=view.instrument,
            model_price=model_price,
            bid=view.bid,
            ask=view.ask,
            strike=view.strike,
            expiry_dt=view.expiry_dt,
            T_years=T,
            cfg=cfg,
        )
        if sig is None:
            return
        self.stats.signals += 1

        if cfg.one_per_instrument and any(
            p.instrument == view.instrument for p in open_positions
        ):
            return
        if len(open_positions) >= cfg.max_open_positions_live:
            return

        portfolio_value = max(
            cfg.total_capital_usd - self.position_log.total_exposure_usd(),
            0.0,
        )
        if cfg.sizing_mode == "kelly":
            requested_qty = kelly_size(
                cfg=cfg,
                model_price=sig.model_price,
                entry_price=sig.entry_price,
                current_portfolio_value=max(portfolio_value, 1.0),
                side=sig.side,
            )
        else:
            requested_qty = flat_size(cfg=cfg, entry_price=sig.entry_price, side=sig.side)

        requested_qty = self._apply_live_size_caps(requested_qty, sig)
        if requested_qty <= 0:
            return

        comparison: ScenarioComparison | None = None
        scenario_decision_label = "n/a"
        scenario_reasons = ""
        inventory_score = 0.0
        inventory_adjustment = 0.0

        candidate_position_proxy = _CandidatePosition(
            instrument=sig.instrument,
            side=sig.side,
            quantity=requested_qty,
            entry_price=sig.entry_price,
            event_ticker=view.event_ticker,
        )

        if scenario_risk_is_active(cfg) or inventory_skew_is_active(cfg):
            try:
                comparison = self._build_comparison(
                    view=view,
                    sig=sig,
                    quantity=requested_qty,
                    open_positions=open_positions,
                    params=params,
                    spot=spot,
                    now=now,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    "scenario comparison failed for %s: %s", view.instrument, exc
                )
                comparison = None

        if inventory_skew_is_active(cfg) and comparison is not None:
            base_required = (
                cfg.effective_buy_min_edge() if sig.side == "buy" else cfg.effective_sell_min_edge()
            )
            decision = evaluate_inventory_skew(
                cfg=cfg,
                raw_edge=sig.edge,
                base_required_edge=base_required,
                comparison=comparison,
                requested_quantity=requested_qty,
            )
            inventory_score = decision.score
            inventory_adjustment = decision.inventory_adjustment
            if not decision.passes_inventory_filter:
                self.logger.info(
                    "inventory skew rejects %s: %s",
                    view.instrument,
                    "; ".join(decision.reasons),
                )
                return
            requested_qty = max(decision.adjusted_quantity, 0)
            if requested_qty <= 0:
                return

        if scenario_risk_is_active(cfg):
            gate = evaluate_candidate_quantity(
                cfg=cfg,
                current_positions=open_positions,
                candidate_position=candidate_position_proxy,
                initial_quantity=requested_qty,
                params=params,
                as_of=now,
                spot_price=spot,
                cache=scenario_cache,
            )
            scenario_decision_label = (
                "accepted" if gate.approved_quantity > 0 else "rejected"
            )
            if gate.decision is not None:
                scenario_reasons = "; ".join(gate.decision.reasons)
            if gate.approved_quantity <= 0:
                self.logger.info(
                    "scenario gate rejects %s: %s",
                    view.instrument,
                    scenario_reasons,
                )
                return
            requested_qty = gate.approved_quantity

        requested_qty = self._apply_live_size_caps(requested_qty, sig)
        if requested_qty <= 0:
            return

        notional = requested_qty * sig.entry_price if sig.side == "buy" else requested_qty * (1.0 - sig.entry_price)
        if notional > cfg.max_notional_per_order_usd:
            scaled = int(cfg.max_notional_per_order_usd / max(
                sig.entry_price if sig.side == "buy" else 1.0 - sig.entry_price,
                1e-9,
            ))
            if scaled <= 0:
                return
            requested_qty = scaled
            notional = requested_qty * (
                sig.entry_price if sig.side == "buy" else 1.0 - sig.entry_price
            )

        total_exposure = self.position_log.total_exposure_usd() + notional
        if total_exposure > cfg.max_total_notional_usd:
            self.logger.info(
                "skipping %s: would exceed max total notional (%.2f > %.2f)",
                view.instrument,
                total_exposure,
                cfg.max_total_notional_usd,
            )
            return

        self._submit_and_record(
            view=view,
            sig=sig,
            quantity=requested_qty,
            now=now,
            scenario_decision=scenario_decision_label,
            scenario_reasons=scenario_reasons,
            inventory_score=inventory_score,
            inventory_adjustment=inventory_adjustment,
        )

    def _apply_live_size_caps(self, quantity: int, sig: Signal) -> int:
        cfg = self.cfg
        capped = min(quantity, cfg.max_quantity_per_order)
        if capped <= 0:
            return 0
        cost_per = sig.entry_price if sig.side == "buy" else 1.0 - sig.entry_price
        if cost_per <= 0:
            return 0
        max_qty_by_notional = int(cfg.max_notional_per_order_usd / cost_per)
        return max(min(capped, max_qty_by_notional), 0)

    def _build_comparison(
        self,
        *,
        view: ContractView,
        sig: Signal,
        quantity: int,
        open_positions: list[Position],
        params: BatesParams,
        spot: float,
        now: dt.datetime,
    ) -> ScenarioComparison:
        current_contracts = contracts_from_positions(open_positions)
        candidate_contract = ScenarioContract(
            instrument=view.instrument,
            side=sig.side,
            quantity=int(quantity),
            entry_price=float(sig.entry_price),
            strike=view.strike,
            expiry_dt=view.expiry_dt,
            event_ticker=view.event_ticker,
        )
        grid = build_scenario_grid(
            as_of=now,
            spot_price=spot,
            contracts=list(current_contracts) + [candidate_contract],
            price_range_pct=self.cfg.scenario_price_range_pct,
            price_step=self.cfg.scenario_price_step,
            time_step_hours=self.cfg.scenario_time_step_hours,
        )
        current_surface = build_portfolio_surface(current_contracts, params=params, grid=grid)
        candidate_addition = build_portfolio_surface(
            [candidate_contract], params=params, grid=grid
        )
        return compare_surface_addition(
            current_surface=current_surface,
            candidate_addition_surface=candidate_addition,
            pin_risk_window_steps=self.cfg.scenario_pin_risk_window_steps,
        )

    # ── order submission ───────────────────────────────────────────────────

    def _submit_and_record(
        self,
        *,
        view: ContractView,
        sig: Signal,
        quantity: int,
        now: dt.datetime,
        scenario_decision: str,
        scenario_reasons: str,
        inventory_score: float,
        inventory_adjustment: float,
    ) -> None:
        cfg = self.cfg
        client_order_id = _make_client_order_id()

        if not cfg.submit_orders:
            self.logger.info(
                "DRY %s %s qty=%d @ %.4f model=%.4f edge=%.4f scenario=%s",
                sig.side.upper(),
                view.instrument,
                quantity,
                sig.entry_price,
                sig.model_price,
                sig.edge,
                scenario_decision,
            )
            self.ledger.append(
                LedgerRow(
                    timestamp=now.isoformat(),
                    client_order_id=client_order_id,
                    order_id="",
                    instrument=view.instrument,
                    event_ticker=view.event_ticker,
                    side=sig.side,
                    outcome=_outcome_for_side(sig.side),
                    requested_quantity=quantity,
                    requested_price=sig.entry_price,
                    filled_quantity=0,
                    avg_fill_price=0.0,
                    fee=0.0,
                    model_price=sig.model_price,
                    edge_at_decision=sig.edge,
                    scenario_decision=scenario_decision,
                    scenario_reasons=scenario_reasons,
                    inventory_score=inventory_score,
                    inventory_adjustment=inventory_adjustment,
                    params_source=self.params.calibration_source,
                    params_calibrated_at=self.params.calibrated_at,
                    spot_at_decision=float(self.params.S),
                    bid_at_decision=view.bid or 0.0,
                    ask_at_decision=view.ask or 0.0,
                    status="dry",
                    notes="submit_orders=false",
                )
            )
            return

        try:
            order = self.client.place_order(
                instrument=view.instrument,
                side=sig.side,
                outcome=_outcome_for_side(sig.side),
                quantity=int(quantity),
                price=float(sig.entry_price),
                client_order_id=client_order_id,
                time_in_force=cfg.time_in_force,
            )
            self._record_api_success()
        except ExecutionError as exc:
            self._record_api_failure(f"place_order: {exc}")
            self.stats.orders_rejected += 1
            self.ledger.append(
                LedgerRow(
                    timestamp=now.isoformat(),
                    client_order_id=client_order_id,
                    order_id="",
                    instrument=view.instrument,
                    event_ticker=view.event_ticker,
                    side=sig.side,
                    outcome=_outcome_for_side(sig.side),
                    requested_quantity=quantity,
                    requested_price=sig.entry_price,
                    filled_quantity=0,
                    avg_fill_price=0.0,
                    fee=0.0,
                    model_price=sig.model_price,
                    edge_at_decision=sig.edge,
                    scenario_decision=scenario_decision,
                    scenario_reasons=scenario_reasons,
                    inventory_score=inventory_score,
                    inventory_adjustment=inventory_adjustment,
                    params_source=self.params.calibration_source,
                    params_calibrated_at=self.params.calibrated_at,
                    spot_at_decision=float(self.params.S),
                    bid_at_decision=view.bid or 0.0,
                    ask_at_decision=view.ask or 0.0,
                    status="error",
                    notes=str(exc),
                )
            )
            return

        self.stats.orders_submitted += 1

        self._handle_fill(
            order=order,
            view=view,
            sig=sig,
            now=now,
            scenario_decision=scenario_decision,
            scenario_reasons=scenario_reasons,
            inventory_score=inventory_score,
            inventory_adjustment=inventory_adjustment,
        )

    def _handle_fill(
        self,
        *,
        order: OrderResult,
        view: ContractView,
        sig: Signal,
        now: dt.datetime,
        scenario_decision: str,
        scenario_reasons: str,
        inventory_score: float,
        inventory_adjustment: float,
    ) -> None:
        filled = order.filled_quantity
        avg_price = order.avg_execution_price or sig.entry_price
        fee = order.fee
        notional = (
            filled * avg_price if sig.side == "buy" else filled * (1.0 - avg_price)
        )

        if filled > 0:
            self.stats.orders_filled += 1
            self.risk.daily_filled_notional_usd += notional
            position = Position(
                instrument=view.instrument,
                event_ticker=view.event_ticker,
                side=sig.side,
                outcome=_outcome_for_side(sig.side),
                quantity=int(filled),
                entry_price=float(avg_price),
                entry_model_price=float(sig.model_price),
                edge_at_entry=float(sig.model_price - avg_price)
                if sig.side == "buy"
                else float(avg_price - sig.model_price),
                entry_time=now.isoformat(),
                expiry_time=view.expiry_dt.isoformat(),
                order_id=order.order_id,
                settlement_index="",
                status="open",
                spot_at_entry=float(self.params.S),
                bid_at_entry=view.bid,
                ask_at_entry=view.ask,
                bid_size_at_entry=view.bid_size,
                ask_size_at_entry=view.ask_size,
                params_path=self.cfg.params_path,
            )
            self.position_log.add(position)
            self.logger.info(
                "FILL %s %s qty=%d @ %.4f notional=%.2f order_id=%s",
                sig.side.upper(),
                view.instrument,
                filled,
                avg_price,
                notional,
                order.order_id,
            )
        else:
            self.logger.info(
                "NO_FILL %s %s qty=%d (status=%s order_id=%s)",
                sig.side.upper(),
                view.instrument,
                order.requested_quantity,
                order.status,
                order.order_id,
            )

        self.ledger.append(
            LedgerRow(
                timestamp=now.isoformat(),
                client_order_id=order.client_order_id,
                order_id=order.order_id,
                instrument=view.instrument,
                event_ticker=view.event_ticker,
                side=sig.side,
                outcome=_outcome_for_side(sig.side),
                requested_quantity=order.requested_quantity or 0,
                requested_price=sig.entry_price,
                filled_quantity=filled,
                avg_fill_price=avg_price,
                fee=fee,
                model_price=sig.model_price,
                edge_at_decision=sig.edge,
                scenario_decision=scenario_decision,
                scenario_reasons=scenario_reasons,
                inventory_score=inventory_score,
                inventory_adjustment=inventory_adjustment,
                params_source=self.params.calibration_source,
                params_calibrated_at=self.params.calibrated_at,
                spot_at_decision=float(self.params.S),
                bid_at_decision=view.bid or 0.0,
                ask_at_decision=view.ask or 0.0,
                status=order.status,
                notes="",
            )
        )


@dataclass
class _CandidatePosition:
    instrument: str
    side: str
    quantity: int
    entry_price: float
    event_ticker: str = ""


# ── entry point ────────────────────────────────────────────────────────────


def _load_params(path: str) -> BatesParams:
    return BatesParams.load(path)


def _refresh_deribit_and_calibrate(
    cfg: StrategyConfig,
    logger: logging.Logger,
) -> BatesParams:
    from calibration.implied import calibrate
    from data_collection.get_deribit_options import collect_and_save

    latest_path, snapshot_path = collect_and_save()
    logger.info(
        "saved Deribit chain snapshot: latest=%s snapshot=%s",
        latest_path,
        snapshot_path,
    )
    return calibrate(
        chain_path=latest_path,
        output_path=cfg.params_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live Gemini BTC market-maker runner")
    parser.add_argument("--config", required=True, help="path to StrategyConfig JSON")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single tick and exit (smoke-test mode)",
    )
    parser.add_argument(
        "--max-loops",
        type=int,
        default=None,
        help="exit after this many loops (smoke-test mode)",
    )
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="force submit_orders=False regardless of config",
    )
    args = parser.parse_args(argv)

    cfg = StrategyConfig.load(args.config)
    if args.no_submit:
        cfg.submit_orders = False

    logger = _setup_logging(cfg.runner_log_path)
    logger.info("loaded config from %s", args.config)

    try:
        params = _load_params(cfg.params_path)
        logger.info("loaded params: %s", params.calibration_source)
    except FileNotFoundError:
        if cfg.calibration_interval_hours <= 0:
            logger.error(
                "params file missing at %s and auto-calibration is disabled",
                cfg.params_path,
            )
            return 2
        logger.warning(
            "params file missing at %s; fetching Deribit chain and calibrating now",
            cfg.params_path,
        )
        try:
            params = _refresh_deribit_and_calibrate(cfg, logger)
        except Exception as exc:  # noqa: BLE001
            logger.error("startup calibration failed: %s\n%s", exc, traceback.format_exc())
            return 2

    client = GeminiExecutionClient(
        base_url=cfg.gemini_base_url,
        fast_api_url=cfg.gemini_fast_api_url,
        fast_api_spot_symbol=cfg.gemini_fast_api_spot_symbol,
        timeout=cfg.request_timeout_seconds,
        dry_run=cfg.dry_run and not cfg.submit_orders,
    )
    position_log = PositionLog(cfg.positions_path)
    ledger = TradeLedger(cfg.trades_log_path)

    runner = LiveRunner(
        cfg=cfg,
        client=client,
        params=params,
        position_log=position_log,
        ledger=ledger,
        logger=logger,
    )

    max_loops = 1 if args.once else args.max_loops
    try:
        runner.run(max_loops=max_loops)
    except RuntimeError as exc:
        logger.error("startup aborted: %s", exc)
        return 2
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
