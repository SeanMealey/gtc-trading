#!/usr/bin/env python3
"""
Streamlit dashboard for the current paper-trading scenario matrix.

Run with:
    streamlit run src/scenario_matrix_dashboard.py
"""

from __future__ import annotations

import datetime as dt
import math
import sys
import urllib.request
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from calibration.params import BatesParams
from strategy.config import StrategyConfig
from strategy.position_log import PositionLog
from strategy.scenario_matrix import (
    build_portfolio_surface,
    build_scenario_grid,
    compute_surface_metrics,
    contract_from_position,
    lognormal_price_probabilities,
)


BASE = "https://api.gemini.com"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "paper.ec2.json"
DEFAULT_PARAMS_FALLBACK = PROJECT_ROOT / "data" / "deribit" / "bates_params_implied.json"


def resolve_project_path(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    raw = Path(path_str).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (PROJECT_ROOT / raw).resolve()


def load_config(config_path_override: str | None = None) -> tuple[StrategyConfig, Path | None]:
    explicit = resolve_project_path(config_path_override)
    if explicit is not None:
        return StrategyConfig.load(str(explicit)), explicit
    if DEFAULT_CONFIG_PATH.exists():
        return StrategyConfig.load(str(DEFAULT_CONFIG_PATH)), DEFAULT_CONFIG_PATH
    return StrategyConfig(), None


def resolve_positions_path(cfg: StrategyConfig, positions_path_override: str | None = None) -> Path:
    explicit = resolve_project_path(positions_path_override)
    if explicit is not None:
        return explicit
    return resolve_project_path(cfg.positions_path) or (PROJECT_ROOT / cfg.positions_path).resolve()


def resolve_params_path(cfg: StrategyConfig, params_path_override: str | None = None) -> Path:
    preferred = resolve_project_path(params_path_override) or resolve_project_path(cfg.params_path)
    if preferred.exists():
        return preferred
    if DEFAULT_PARAMS_FALLBACK.exists():
        return DEFAULT_PARAMS_FALLBACK.resolve()
    raise FileNotFoundError(
        f"Could not find Bates params file at {preferred} or {DEFAULT_PARAMS_FALLBACK}"
    )


def fetch_live_spot_price() -> float | None:
    try:
        with urllib.request.urlopen(f"{BASE}/v1/pubticker/BTCUSD", timeout=5) as response:
            payload = response.read().decode("utf-8")
    except Exception:
        return None

    try:
        import json

        data = json.loads(payload)
        return float(data["last"])
    except Exception:
        return None


def resolve_spot_price() -> tuple[float | None, str]:
    live_spot = fetch_live_spot_price()
    if live_spot is not None:
        st.session_state["last_live_btc_spot"] = float(live_spot)
        return float(live_spot), "gemini api"

    cached_spot = st.session_state.get("last_live_btc_spot")
    if cached_spot is not None:
        return float(cached_spot), "cached live api"

    return None, "unavailable"


def load_live_positions(
    config_path_override: str | None = None,
    positions_path_override: str | None = None,
) -> tuple[list, list]:
    cfg, _ = load_config(config_path_override)
    position_log = PositionLog(str(resolve_positions_path(cfg, positions_path_override)))
    positions = position_log.open_positions()
    now = dt.datetime.now(dt.timezone.utc)

    active_positions = []
    expired_open_positions = []
    for position in positions:
        expiry_dt = dt.datetime.fromisoformat(position.expiry_time.replace("Z", "+00:00"))
        if expiry_dt > now:
            active_positions.append(position)
        else:
            expired_open_positions.append(position)
    return active_positions, expired_open_positions


def build_positions_frame(positions: list) -> pd.DataFrame:
    rows = []
    for position in positions:
        rows.append(
            {
                "instrument": position.instrument,
                "side": position.side.upper(),
                "qty": int(position.quantity),
                "entry_price": float(position.entry_price),
                "entry_model": float(position.entry_model_price),
                "edge": float(position.edge_at_entry),
                "entry_spot": float(position.spot_at_entry) if position.spot_at_entry is not None else None,
                "expiry_utc": position.expiry_time.replace("T", " ").replace("Z", " UTC"),
            }
        )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame.sort_values(["expiry_utc", "instrument"], inplace=True)
    return frame


def auto_refresh(interval_seconds: int) -> None:
    if interval_seconds <= 0:
        return
    st.components.v1.html(
        f"""
        <script>
        setTimeout(function() {{
            window.parent.location.reload();
        }}, {int(interval_seconds * 1000)});
        </script>
        """,
        height=0,
    )


def render_live_dashboard(
    config_path_override: str | None,
    positions_path_override: str | None,
    params_path_override: str | None,
    price_range_pct: float,
    price_step: int,
    time_step_hours: int,
    hours_past_expiry: int,
) -> None:
    cfg, _ = load_config(config_path_override)
    params_path = resolve_params_path(cfg, params_path_override)
    params = BatesParams.load(str(params_path))

    active_positions, expired_open_positions = load_live_positions(
        config_path_override=config_path_override,
        positions_path_override=positions_path_override,
    )
    if expired_open_positions:
        st.warning(
            f"{len(expired_open_positions)} open positions are already past expiry and were excluded from the live matrix."
        )

    if not active_positions:
        st.info("No active open positions found in the paper portfolio.")
        return

    as_of = dt.datetime.now(dt.timezone.utc)
    spot_price, spot_source = resolve_spot_price()
    if spot_price is None:
        st.error("Live BTC spot is unavailable and no prior API price has been cached yet.")
        return
    contracts = [contract_from_position(position) for position in active_positions]
    last_expiry = max(contract.expiry_dt for contract in contracts)
    horizon_dt = max(last_expiry + dt.timedelta(hours=hours_past_expiry), as_of)
    grid = build_scenario_grid(
        as_of=as_of,
        spot_price=spot_price,
        contracts=contracts,
        price_range_pct=price_range_pct,
        price_step=float(price_step),
        time_step_hours=float(time_step_hours),
        horizon_dt=horizon_dt,
    )
    surface = build_portfolio_surface(contracts=contracts, params=params, grid=grid)

    terminal_horizon_years = max((grid.evaluation_times[-1] - as_of).total_seconds(), 3600.0) / (365.25 * 24 * 3600)
    sigma = max(math.sqrt(max(params.v0, 1e-8)), 0.05)
    probabilities = lognormal_price_probabilities(
        spot_price=spot_price,
        prices=grid.prices,
        horizon_years=terminal_horizon_years,
        sigma=sigma,
        risk_free_rate=params.r,
    )
    metrics = compute_surface_metrics(surface, probability_weights=probabilities)

    positions_frame = build_positions_frame(active_positions)
    net_contracts = sum(position.quantity if position.side == "buy" else -position.quantity for position in active_positions)

    summary_cols = st.columns(6)
    summary_cols[0].metric("BTC Spot", f"{spot_price:,.2f}", help=f"Source: {spot_source}")
    summary_cols[1].metric("Open Positions", f"{len(active_positions)}")
    summary_cols[2].metric("Net Contracts", f"{net_contracts:+d}")
    summary_cols[3].metric("Expected P&L", f"{metrics.expected_pnl:.2f}" if metrics.expected_pnl is not None else "n/a")
    summary_cols[4].metric("Worst Case", f"{metrics.max_loss:.2f}")
    summary_cols[5].metric("Terminal Flatness", f"{metrics.surface_flatness_terminal:.2f}")

    secondary_cols = st.columns(4)
    secondary_cols[0].metric("Worst BTC", f"{metrics.max_loss_price:,.0f}")
    secondary_cols[1].metric("Worst Time", metrics.max_loss_time.strftime("%m-%d %H:%M UTC"))
    secondary_cols[2].metric("Negative Terminal Cells", str(metrics.terminal_negative_cells))
    secondary_cols[3].metric("Payoff Variance", f"{metrics.payoff_variance:.2f}")

    st.plotly_chart(
        create_heatmap(
            prices=grid.prices,
            evaluation_times=grid.evaluation_times,
            pnl_matrix=surface.pnl,
            spot_price=spot_price,
        ),
        use_container_width=True,
    )

    st.plotly_chart(
        create_profile_chart(
            prices=grid.prices,
            terminal_pnl=surface.pnl[-1],
            terminal_delta=metrics.delta[-1],
            spot_price=spot_price,
            positions=active_positions,
        ),
        use_container_width=True,
    )

    left, right = st.columns([1.2, 1.0])
    with left:
        st.subheader("Open Positions")
        st.dataframe(positions_frame, use_container_width=True, hide_index=True)
    with right:
        st.subheader("Holes")
        if metrics.hole_ranges:
            holes_frame = pd.DataFrame(
                [
                    {
                        "start_price": hole.start_price,
                        "end_price": hole.end_price,
                        "worst_pnl": hole.worst_pnl,
                    }
                    for hole in metrics.hole_ranges
                ]
            )
            st.dataframe(holes_frame, use_container_width=True, hide_index=True)
        else:
            st.success("No negative terminal ranges detected on the current spot-centered grid.")

    st.caption(
        "The scenario matrix is always centered on current BTC spot and re-priced across time to expiry "
        "for the active paper portfolio."
    )


def create_heatmap(prices, evaluation_times, pnl_matrix, spot_price: float) -> go.Figure:
    time_labels = [ts.strftime("%Y-%m-%d %H:%M UTC") for ts in evaluation_times]
    fig = go.Figure(
        data=
        [
            go.Heatmap(
                x=prices,
                y=time_labels,
                z=pnl_matrix,
                colorscale="RdYlGn",
                zmid=0.0,
                colorbar={"title": "P&L"},
                hovertemplate="Time=%{y}<br>BTC=%{x:,.0f}<br>P&L=%{z:.4f}<extra></extra>",
            )
        ]
    )
    fig.add_vline(
        x=spot_price,
        line_dash="dash",
        line_color="#1d4ed8",
        annotation_text=f"spot {spot_price:,.0f}",
    )
    fig.update_layout(
        template="plotly_white",
        height=560,
        margin={"l": 20, "r": 20, "t": 40, "b": 20},
        xaxis_title="BTC Price",
        yaxis_title="Evaluation Time",
    )
    fig.update_xaxes(tickformat=",.0f")
    return fig


def create_profile_chart(prices, terminal_pnl, terminal_delta, spot_price: float, positions: list) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=prices,
            y=terminal_pnl,
            mode="lines",
            name="Terminal P&L",
            line={"color": "#0f766e", "width": 3},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=prices,
            y=terminal_delta,
            mode="lines",
            name="Terminal Delta",
            yaxis="y2",
            line={"color": "#b45309", "width": 3},
        )
    )
    fig.add_vline(
        x=spot_price,
        line_dash="dash",
        line_color="#1d4ed8",
        annotation_text=f"spot {spot_price:,.0f}",
    )
    for position in positions:
        strike = float(position.instrument.split("-HI")[-1])
        fig.add_vline(
            x=strike,
            line_dash="dot",
            line_color="#6b7280",
            opacity=0.55,
        )

    fig.update_layout(
        template="plotly_white",
        height=420,
        margin={"l": 20, "r": 20, "t": 40, "b": 20},
        xaxis={"title": "BTC Price", "tickformat": ",.0f"},
        yaxis={"title": "Terminal P&L"},
        yaxis2={
            "title": "Terminal Delta",
            "overlaying": "y",
            "side": "right",
        },
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0.0},
    )
    return fig


def main() -> None:
    st.set_page_config(
        page_title="BTC Scenario Matrix",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("BTC Paper Portfolio Scenario Matrix")

    default_config_value = str(DEFAULT_CONFIG_PATH.relative_to(PROJECT_ROOT)) if DEFAULT_CONFIG_PATH.exists() else ""

    with st.sidebar:
        st.header("Controls")
        config_path_override = st.text_input("Config path", value=default_config_value)
        positions_path_override = st.text_input("Positions path override", value="")
        params_path_override = st.text_input("Params path override", value="")
        refresh_seconds = st.slider("Auto-refresh seconds", min_value=0, max_value=60, value=10, step=5)
        price_range_pct = st.slider("Price range around current BTC", min_value=0.02, max_value=0.20, value=0.08, step=0.01)
        price_step = st.slider("Price step (USD)", min_value=50, max_value=1000, value=100, step=50)
        time_step_hours = st.slider("Time step (hours)", min_value=1, max_value=6, value=1, step=1)
        hours_past_expiry = st.slider("Hours past final expiry", min_value=0, max_value=6, value=2, step=1)
        cfg, config_path = load_config(config_path_override or None)
        effective_positions_path = resolve_positions_path(cfg, positions_path_override or None)
        effective_params_path = resolve_params_path(cfg, params_path_override or None)
        st.caption(f"Config: {config_path if config_path else 'default StrategyConfig values'}")
        st.caption(f"Positions: {effective_positions_path}")
        st.caption(f"Params: {effective_params_path}")
        if st.button("Refresh Now", use_container_width=True):
            st.rerun()

    fragment_api = getattr(st, "fragment", None)
    if fragment_api is not None:
        if refresh_seconds > 0:
            @fragment_api(run_every=f"{refresh_seconds}s")
            def live_fragment() -> None:
                render_live_dashboard(
                    config_path_override=config_path_override or None,
                    positions_path_override=positions_path_override or None,
                    params_path_override=params_path_override or None,
                    price_range_pct=price_range_pct,
                    price_step=price_step,
                    time_step_hours=time_step_hours,
                    hours_past_expiry=hours_past_expiry,
                )
        else:
            @fragment_api
            def live_fragment() -> None:
                render_live_dashboard(
                    config_path_override=config_path_override or None,
                    positions_path_override=positions_path_override or None,
                    params_path_override=params_path_override or None,
                    price_range_pct=price_range_pct,
                    price_step=price_step,
                    time_step_hours=time_step_hours,
                    hours_past_expiry=hours_past_expiry,
                )

        live_fragment()
    else:
        st.info(
            "This Streamlit version does not support partial fragment reruns yet, so the app will fall back "
            "to full-page refreshes for live updates."
        )
        auto_refresh(refresh_seconds)
        render_live_dashboard(
            config_path_override=config_path_override or None,
            positions_path_override=positions_path_override or None,
            params_path_override=params_path_override or None,
            price_range_pct=price_range_pct,
            price_step=price_step,
            time_step_hours=time_step_hours,
            hours_past_expiry=hours_past_expiry,
        )


if __name__ == "__main__":
    main()
