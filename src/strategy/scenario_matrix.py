"""
Scenario-matrix portfolio simulation for Gemini BTC binary options.

This module values the current portfolio and candidate trades across a grid of
BTC prices and evaluation times. The resulting surface is expressed as P&L
relative to trade entry prices so it can be used directly for risk controls.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import sys
from dataclasses import dataclass

import numpy as np

from calibration.params import BatesParams


SECONDS_PER_YEAR = 365.25 * 24 * 3600
INSTRUMENT_RE = re.compile(r"^GEMI-BTC(?P<event>\d{10})-HI(?P<strike>\d+)$")
ASCII_HEATMAP_LEVELS = " .:-=+*#%@"


@dataclass(frozen=True)
class ScenarioContract:
    instrument: str
    side: str
    quantity: int
    entry_price: float
    strike: float
    expiry_dt: dt.datetime
    event_ticker: str = ""


@dataclass(frozen=True)
class ScenarioGrid:
    prices: np.ndarray
    evaluation_times: tuple[dt.datetime, ...]

    @property
    def time_offsets_hours(self) -> np.ndarray:
        start = self.evaluation_times[0]
        offsets = [
            (evaluation_time - start).total_seconds() / 3600.0
            for evaluation_time in self.evaluation_times
        ]
        return np.asarray(offsets, dtype=float)


@dataclass(frozen=True)
class ScenarioSurface:
    grid: ScenarioGrid
    pnl: np.ndarray


@dataclass(frozen=True)
class HoleRange:
    start_price: float
    end_price: float
    worst_pnl: float


@dataclass(frozen=True)
class ScenarioMetrics:
    flatness_by_time: np.ndarray
    surface_flatness_mean: float
    surface_flatness_terminal: float
    max_loss: float
    max_loss_price: float
    max_loss_time: dt.datetime
    payoff_variance: float
    expected_pnl: float | None
    terminal_negative_cells: int
    negative_cells: int
    hole_ranges: tuple[HoleRange, ...]
    delta: np.ndarray
    theta: np.ndarray


@dataclass(frozen=True)
class ScenarioComparison:
    grid: ScenarioGrid
    current_surface: ScenarioSurface
    candidate_surface: ScenarioSurface
    current_metrics: ScenarioMetrics
    candidate_metrics: ScenarioMetrics
    flatness_improved: bool
    variance_improved: bool
    max_loss_improved: bool
    hole_reduced: bool
    expected_pnl_improved: bool | None


@dataclass(frozen=True)
class ScenarioRiskLimits:
    max_surface_flatness: float | None = None
    max_terminal_negative_cells: int | None = None
    max_payoff_variance: float | None = None
    min_expected_pnl: float | None = None
    min_max_loss: float | None = None
    require_flatness_improvement: bool = False
    require_variance_improvement: bool = False
    require_hole_reduction: bool = False


@dataclass(frozen=True)
class ScenarioDecision:
    accepted: bool
    reasons: tuple[str, ...]


def _load_pricer():
    pricer_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pricer")
    if pricer_dir not in sys.path:
        sys.path.insert(0, pricer_dir)
    import bates_pricer as bp

    return bp


def parse_instrument(symbol: str) -> tuple[str, dt.datetime, float] | None:
    match = INSTRUMENT_RE.match(symbol)
    if not match:
        return None

    event_code = match.group("event")
    expiry = dt.datetime(
        int("20" + event_code[0:2]),
        int(event_code[2:4]),
        int(event_code[4:6]),
        int(event_code[6:8]),
        int(event_code[8:10]),
        tzinfo=dt.timezone.utc,
    )
    return f"BTC{event_code}", expiry, float(match.group("strike"))


def contract_from_position(position) -> ScenarioContract:
    parsed = parse_instrument(position.instrument)
    if parsed is None:
        raise ValueError(f"unsupported instrument format: {position.instrument}")
    event_ticker, expiry_dt, strike = parsed
    return ScenarioContract(
        instrument=position.instrument,
        side=position.side,
        quantity=int(position.quantity),
        entry_price=float(position.entry_price),
        strike=strike,
        expiry_dt=expiry_dt,
        event_ticker=getattr(position, "event_ticker", event_ticker),
    )


def build_scenario_grid(
    as_of: dt.datetime,
    spot_price: float,
    contracts: list[ScenarioContract] | tuple[ScenarioContract, ...],
    price_range_pct: float = 0.15,
    price_step: float = 250.0,
    time_step_hours: float = 4.0,
    horizon_dt: dt.datetime | None = None,
) -> ScenarioGrid:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if spot_price <= 0:
        raise ValueError("spot_price must be positive")
    if price_step <= 0 or time_step_hours <= 0:
        raise ValueError("price_step and time_step_hours must be positive")

    epsilon = price_step * 1e-9
    lower = max(
        price_step,
        math.floor((spot_price * (1.0 - price_range_pct) + epsilon) / price_step) * price_step,
    )
    upper = math.ceil((spot_price * (1.0 + price_range_pct) - epsilon) / price_step) * price_step
    prices = np.arange(lower, upper + (price_step * 0.5), price_step, dtype=float)

    if horizon_dt is None:
        if contracts:
            last_expiry = max(contract.expiry_dt for contract in contracts)
            horizon_dt = max(last_expiry, as_of + dt.timedelta(days=5))
        else:
            horizon_dt = as_of + dt.timedelta(days=5)

    evaluation_times = [as_of]
    step = dt.timedelta(hours=time_step_hours)
    cursor = as_of
    while cursor < horizon_dt:
        cursor = min(cursor + step, horizon_dt)
        evaluation_times.append(cursor)

    return ScenarioGrid(prices=prices, evaluation_times=tuple(evaluation_times))


def _binary_yes_price(
    params: BatesParams,
    strike: float,
    time_to_expiry_years: float,
    simulated_spot: float,
) -> float:
    bp = _load_pricer()
    pricer_params = params.to_pricer_params()
    pricer_params.S = float(simulated_spot)
    return float(bp.binary_call(float(strike), float(time_to_expiry_years), pricer_params, N=256))


def contract_pnl_at_node(
    contract: ScenarioContract,
    params: BatesParams,
    simulated_spot: float,
    evaluation_time: dt.datetime,
) -> float:
    if evaluation_time >= contract.expiry_dt:
        contract_value = 1.0 if simulated_spot >= contract.strike else 0.0
    else:
        time_to_expiry_years = max(
            (contract.expiry_dt - evaluation_time).total_seconds(),
            0.0,
        ) / SECONDS_PER_YEAR
        contract_value = _binary_yes_price(
            params=params,
            strike=contract.strike,
            time_to_expiry_years=time_to_expiry_years,
            simulated_spot=simulated_spot,
        )

    if contract.side == "buy":
        pnl_per_contract = contract_value - contract.entry_price
    elif contract.side == "sell":
        pnl_per_contract = contract.entry_price - contract_value
    else:
        raise ValueError(f"unsupported side: {contract.side!r}")

    return pnl_per_contract * contract.quantity


def build_portfolio_surface(
    contracts: list[ScenarioContract] | tuple[ScenarioContract, ...],
    params: BatesParams,
    grid: ScenarioGrid,
) -> ScenarioSurface:
    surface = np.zeros((len(grid.evaluation_times), len(grid.prices)), dtype=float)
    if not contracts:
        return ScenarioSurface(grid=grid, pnl=surface)

    bp = _load_pricer()
    pricer_params = params.to_pricer_params()
    price_axis = np.asarray(grid.prices, dtype=float)

    for contract in contracts:
        for time_idx, evaluation_time in enumerate(grid.evaluation_times):
            if evaluation_time >= contract.expiry_dt:
                contract_values = (price_axis >= contract.strike).astype(float)
            else:
                time_to_expiry_years = max(
                    (contract.expiry_dt - evaluation_time).total_seconds(),
                    0.0,
                ) / SECONDS_PER_YEAR
                contract_values = np.asarray(
                    bp.binary_call_batch(
                        float(contract.strike),
                        float(time_to_expiry_years),
                        pricer_params,
                        price_axis,
                        N=256,
                    ),
                    dtype=float,
                )

            if contract.side == "buy":
                surface[time_idx] += (contract_values - contract.entry_price) * contract.quantity
            elif contract.side == "sell":
                surface[time_idx] += (contract.entry_price - contract_values) * contract.quantity
            else:
                raise ValueError(f"unsupported side: {contract.side!r}")
    return ScenarioSurface(grid=grid, pnl=surface)


def _central_difference(values: np.ndarray, coordinates: np.ndarray, axis: int) -> np.ndarray:
    return np.gradient(values, coordinates, axis=axis, edge_order=1)


def _normalize_probabilities(probabilities: np.ndarray) -> np.ndarray:
    clipped = np.clip(probabilities.astype(float), 0.0, None)
    total = clipped.sum()
    if total <= 0:
        raise ValueError("probabilities must sum to a positive number")
    return clipped / total


def detect_hole_ranges(pnl_row: np.ndarray, prices: np.ndarray) -> tuple[HoleRange, ...]:
    holes: list[HoleRange] = []
    start_idx: int | None = None
    worst_pnl = 0.0

    for idx, value in enumerate(pnl_row):
        if value < 0 and start_idx is None:
            start_idx = idx
            worst_pnl = float(value)
            continue
        if value < 0 and start_idx is not None:
            worst_pnl = min(worst_pnl, float(value))
            continue
        if value >= 0 and start_idx is not None:
            holes.append(
                HoleRange(
                    start_price=float(prices[start_idx]),
                    end_price=float(prices[idx - 1]),
                    worst_pnl=worst_pnl,
                )
            )
            start_idx = None

    if start_idx is not None:
        holes.append(
            HoleRange(
                start_price=float(prices[start_idx]),
                end_price=float(prices[-1]),
                worst_pnl=worst_pnl,
            )
        )

    return tuple(holes)


def compute_surface_metrics(
    surface: ScenarioSurface,
    probability_weights: np.ndarray | None = None,
    target_time_index: int = -1,
) -> ScenarioMetrics:
    pnl = surface.pnl
    grid = surface.grid
    row_stds = pnl.std(axis=1)
    target_row = pnl[target_time_index]

    if probability_weights is not None:
        weights = _normalize_probabilities(probability_weights)
        expected_pnl = float(np.dot(target_row, weights))
        payoff_variance = float(np.dot((target_row - expected_pnl) ** 2, weights))
    else:
        expected_pnl = None
        payoff_variance = float(np.var(target_row))

    min_idx = int(np.argmin(pnl))
    min_time_idx, min_price_idx = np.unravel_index(min_idx, pnl.shape)
    delta = _central_difference(pnl, grid.prices, axis=1)
    theta = _central_difference(pnl, grid.time_offsets_hours, axis=0)
    holes = detect_hole_ranges(target_row, grid.prices)

    return ScenarioMetrics(
        flatness_by_time=row_stds,
        surface_flatness_mean=float(row_stds.mean()),
        surface_flatness_terminal=float(row_stds[target_time_index]),
        max_loss=float(pnl[min_time_idx, min_price_idx]),
        max_loss_price=float(grid.prices[min_price_idx]),
        max_loss_time=grid.evaluation_times[min_time_idx],
        payoff_variance=payoff_variance,
        expected_pnl=expected_pnl,
        terminal_negative_cells=int((target_row < 0).sum()),
        negative_cells=int((pnl < 0).sum()),
        hole_ranges=holes,
        delta=delta,
        theta=theta,
    )


def lognormal_price_probabilities(
    spot_price: float,
    prices: np.ndarray,
    horizon_years: float,
    sigma: float,
    risk_free_rate: float = 0.0,
) -> np.ndarray:
    if spot_price <= 0 or horizon_years <= 0 or sigma <= 0:
        raise ValueError("spot_price, horizon_years, and sigma must be positive")

    sigma_root_t = sigma * math.sqrt(horizon_years)
    mu = math.log(spot_price) + (risk_free_rate - 0.5 * sigma * sigma) * horizon_years

    edges = np.empty(len(prices) + 1, dtype=float)
    if len(prices) == 1:
        half_step = max(prices[0] * 0.01, 1.0)
        edges[0] = max(prices[0] - half_step, 1e-9)
        edges[1] = prices[0] + half_step
    else:
        mids = (prices[:-1] + prices[1:]) / 2.0
        edges[1:-1] = mids
        edges[0] = max(prices[0] - (mids[0] - prices[0]), 1e-9)
        edges[-1] = prices[-1] + (prices[-1] - mids[-1])

    def cdf(x: float) -> float:
        if x <= 0:
            return 0.0
        z = (math.log(x) - mu) / sigma_root_t
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    probabilities = np.empty(len(prices), dtype=float)
    for idx in range(len(prices)):
        probabilities[idx] = max(cdf(edges[idx + 1]) - cdf(edges[idx]), 0.0)

    total = probabilities.sum()
    if total <= 0:
        nearest_idx = int(np.argmin(np.abs(prices - spot_price)))
        probabilities = np.zeros(len(prices), dtype=float)
        probabilities[nearest_idx] = 1.0
        return probabilities
    return probabilities / total


def bates_implied_price_probabilities(
    params: BatesParams,
    spot_price: float,
    prices: np.ndarray,
    horizon_years: float,
    N: int = 256,
) -> np.ndarray:
    if spot_price <= 0:
        raise ValueError("spot_price must be positive")
    if len(prices) == 0:
        raise ValueError("prices must not be empty")
    if len(prices) == 1 or horizon_years <= 0:
        probabilities = np.zeros(len(prices), dtype=float)
        nearest_idx = int(np.argmin(np.abs(prices - spot_price)))
        probabilities[nearest_idx] = 1.0
        return probabilities

    bp = _load_pricer()
    pricer_params = params.to_pricer_params()
    pricer_params.S = float(spot_price)

    mids = (prices[:-1] + prices[1:]) / 2.0
    tail_probabilities = np.asarray(
        [
            bp.binary_call_prob(float(strike), float(horizon_years), pricer_params, N=N)
            for strike in mids
        ],
        dtype=float,
    )
    tail_probabilities = np.clip(tail_probabilities, 0.0, 1.0)
    tail_probabilities = np.minimum.accumulate(tail_probabilities)

    probabilities = np.empty(len(prices), dtype=float)
    probabilities[0] = 1.0 - tail_probabilities[0]
    if len(prices) > 2:
        probabilities[1:-1] = tail_probabilities[:-1] - tail_probabilities[1:]
    probabilities[-1] = tail_probabilities[-1]
    probabilities = np.clip(probabilities, 0.0, None)
    return _normalize_probabilities(probabilities)


def compare_candidate_trade(
    current_contracts: list[ScenarioContract] | tuple[ScenarioContract, ...],
    candidate_contract: ScenarioContract,
    params: BatesParams,
    as_of: dt.datetime,
    spot_price: float,
    price_range_pct: float = 0.15,
    price_step: float = 250.0,
    time_step_hours: float = 4.0,
    horizon_dt: dt.datetime | None = None,
    probability_weights: np.ndarray | None = None,
) -> ScenarioComparison:
    combined_contracts = list(current_contracts) + [candidate_contract]
    grid = build_scenario_grid(
        as_of=as_of,
        spot_price=spot_price,
        contracts=combined_contracts,
        price_range_pct=price_range_pct,
        price_step=price_step,
        time_step_hours=time_step_hours,
        horizon_dt=horizon_dt,
    )
    current_surface = build_portfolio_surface(list(current_contracts), params=params, grid=grid)
    candidate_surface = build_portfolio_surface(combined_contracts, params=params, grid=grid)
    current_metrics = compute_surface_metrics(current_surface, probability_weights=probability_weights)
    candidate_metrics = compute_surface_metrics(candidate_surface, probability_weights=probability_weights)

    expected_pnl_improved: bool | None
    if current_metrics.expected_pnl is None or candidate_metrics.expected_pnl is None:
        expected_pnl_improved = None
    else:
        expected_pnl_improved = candidate_metrics.expected_pnl >= current_metrics.expected_pnl

    return ScenarioComparison(
        grid=grid,
        current_surface=current_surface,
        candidate_surface=candidate_surface,
        current_metrics=current_metrics,
        candidate_metrics=candidate_metrics,
        flatness_improved=(
            candidate_metrics.surface_flatness_terminal
            <= current_metrics.surface_flatness_terminal
        ),
        variance_improved=candidate_metrics.payoff_variance <= current_metrics.payoff_variance,
        max_loss_improved=candidate_metrics.max_loss >= current_metrics.max_loss,
        hole_reduced=(
            candidate_metrics.terminal_negative_cells
            <= current_metrics.terminal_negative_cells
        ),
        expected_pnl_improved=expected_pnl_improved,
    )


def decide_candidate_trade(
    comparison: ScenarioComparison,
    limits: ScenarioRiskLimits,
) -> ScenarioDecision:
    reasons: list[str] = []
    metrics = comparison.candidate_metrics

    if limits.max_surface_flatness is not None and metrics.surface_flatness_terminal > limits.max_surface_flatness:
        reasons.append(
            f"terminal flatness {metrics.surface_flatness_terminal:.4f} exceeds limit {limits.max_surface_flatness:.4f}"
        )
    if limits.max_terminal_negative_cells is not None and metrics.terminal_negative_cells > limits.max_terminal_negative_cells:
        reasons.append(
            f"terminal negative cells {metrics.terminal_negative_cells} exceeds limit {limits.max_terminal_negative_cells}"
        )
    if limits.max_payoff_variance is not None and metrics.payoff_variance > limits.max_payoff_variance:
        reasons.append(
            f"payoff variance {metrics.payoff_variance:.4f} exceeds limit {limits.max_payoff_variance:.4f}"
        )
    if limits.min_expected_pnl is not None:
        if metrics.expected_pnl is None or metrics.expected_pnl < limits.min_expected_pnl:
            reasons.append(
                f"expected pnl {0.0 if metrics.expected_pnl is None else metrics.expected_pnl:.4f} below minimum {limits.min_expected_pnl:.4f}"
            )
    if limits.min_max_loss is not None and metrics.max_loss < limits.min_max_loss:
        reasons.append(
            f"max loss {metrics.max_loss:.4f} below floor {limits.min_max_loss:.4f}"
        )
    if limits.require_flatness_improvement and not comparison.flatness_improved:
        reasons.append("candidate trade does not improve surface flatness")
    if limits.require_variance_improvement and not comparison.variance_improved:
        reasons.append("candidate trade does not improve payoff variance")
    if limits.require_hole_reduction and not comparison.hole_reduced:
        reasons.append("candidate trade does not reduce terminal hole count")

    return ScenarioDecision(accepted=not reasons, reasons=tuple(reasons))


def render_ascii_heatmap(
    surface: ScenarioSurface,
    width: int | None = None,
    levels: str = ASCII_HEATMAP_LEVELS,
) -> str:
    pnl = surface.pnl
    prices = surface.grid.prices
    times = surface.grid.evaluation_times
    if width is not None and width > 0 and pnl.shape[1] > width:
        sample_idx = np.linspace(0, pnl.shape[1] - 1, width).round().astype(int)
        sample_idx = np.unique(sample_idx)
        pnl = pnl[:, sample_idx]
        prices = prices[sample_idx]

    low = float(np.min(pnl))
    high = float(np.max(pnl))
    if math.isclose(low, high):
        normalized = np.full_like(pnl, fill_value=len(levels) // 2, dtype=int)
    else:
        scaled = (pnl - low) / (high - low)
        normalized = np.clip(
            np.rint(scaled * (len(levels) - 1)).astype(int),
            0,
            len(levels) - 1,
        )

    header = (
        f"price {prices[0]:,.0f} -> {prices[-1]:,.0f} | "
        f"pnl {low:+.3f} -> {high:+.3f}"
    )
    lines = [header]
    for time_idx, evaluation_time in enumerate(times):
        row = "".join(levels[level_idx] for level_idx in normalized[time_idx])
        label = evaluation_time.strftime("%m-%d %H:%M")
        lines.append(f"{label} {row}")
    return "\n".join(lines)


def summarize_metrics(metrics: ScenarioMetrics) -> str:
    expected = "n/a" if metrics.expected_pnl is None else f"{metrics.expected_pnl:+.4f}"
    holes = "none"
    if metrics.hole_ranges:
        holes = ", ".join(
            f"{hole.start_price:,.0f}-{hole.end_price:,.0f} ({hole.worst_pnl:+.3f})"
            for hole in metrics.hole_ranges
        )
    return (
        f"flatness_terminal={metrics.surface_flatness_terminal:.4f} "
        f"flatness_mean={metrics.surface_flatness_mean:.4f} "
        f"max_loss={metrics.max_loss:+.4f} "
        f"expected_pnl={expected} "
        f"variance={metrics.payoff_variance:.4f} "
        f"terminal_negative_cells={metrics.terminal_negative_cells} "
        f"holes={holes}"
    )
