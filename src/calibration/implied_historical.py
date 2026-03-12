"""
Historical Bates calibration from reconstructed daily chain snapshots.

This is separate from the live calibrator in implied.py. The main difference is
that liquidity weighting uses a trade-based proxy (`contracts_traded_lookback`
or `open_interest`) rather than true Deribit open interest snapshots.
"""

from __future__ import annotations

import argparse
import math
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution, minimize
from scipy.stats import norm

from params import BatesParams
from implied import (
    _cos_precompute,
    _vanilla_calls_batch,
    infer_snapshot_timestamp,
)


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(THIS_DIR, "../.."))
DEFAULT_CHAIN_PATH = os.path.join(ROOT, "data", "deribit", "historical_chains", "btc_options_chain_20260224_235959.csv")
DEFAULT_OUTPUT_PATH = os.path.join(ROOT, "data", "deribit", "params_history", "bates_params_20260224_235959.json")


class _CalPoint(tuple):
    __slots__ = ()

    @property
    def K(self) -> float:
        return self[0]

    @property
    def T(self) -> float:
        return self[1]

    @property
    def F(self) -> float:
        return self[2]

    @property
    def market_iv(self) -> float:
        return self[3]

    @property
    def weight(self) -> float:
        return self[4]


def _weight_column(df: pd.DataFrame) -> pd.Series:
    if "contracts_traded_lookback" in df.columns:
        return pd.to_numeric(df["contracts_traded_lookback"], errors="coerce").fillna(0.0)
    return pd.to_numeric(df["open_interest"], errors="coerce").fillna(0.0)


def _build_historical_cal_set(
    df: pd.DataFrame,
    min_liquidity: float = 1.0,
    min_tte_years: float = 3.0 / 365.25,
    min_mark_price_btc: float = 0.001,
    min_iv_pct: float = 1.0,
    max_iv_pct: float = 300.0,
    mono_lo: float = 0.75,
    mono_hi: float = 1.35,
    max_per_expiry: int = 20,
) -> list[_CalPoint]:
    df = df.copy()
    df["option_type"] = df["option_type"].astype(str)
    df["mark_iv"] = pd.to_numeric(df["mark_iv"], errors="coerce")
    df["mark_price_btc"] = pd.to_numeric(df["mark_price_btc"], errors="coerce")
    df["underlying_price"] = pd.to_numeric(df["underlying_price"], errors="coerce")
    df["tte_years"] = pd.to_numeric(df["tte_years"], errors="coerce")
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["liq_weight"] = _weight_column(df)

    df = df[df["option_type"] == "call"]
    df = df[df["mark_iv"].notna() & (df["mark_iv"] >= min_iv_pct) & (df["mark_iv"] <= max_iv_pct)]
    df = df[df["mark_price_btc"].notna() & (df["mark_price_btc"] >= min_mark_price_btc)]
    df = df[df["liq_weight"] >= min_liquidity]
    df = df[df["tte_years"] >= min_tte_years]
    df = df[df["underlying_price"].notna() & (df["underlying_price"] > 0)]

    df["moneyness"] = df["strike"] / df["underlying_price"]
    df = df[(df["moneyness"] >= mono_lo) & (df["moneyness"] <= mono_hi)]

    df = (
        df.sort_values("liq_weight", ascending=False)
        .groupby("expiry_dt", sort=False)
        .head(max_per_expiry)
        .reset_index(drop=True)
    )

    points: list[_CalPoint] = []
    for _, row in df.iterrows():
        points.append(
            _CalPoint(
                (
                    float(row["strike"]),
                    float(row["tte_years"]),
                    float(row["underlying_price"]),
                    float(row["mark_iv"]) / 100.0,
                    math.sqrt(max(float(row["liq_weight"]), 1.0)),
                )
            )
        )
    return points


def _bs_iv_batch_historical(
    prices: np.ndarray,
    F_arr: np.ndarray,
    K_arr: np.ndarray,
    T: float,
    r: float,
    n_iter: int = 20,
) -> np.ndarray:
    """
    Safer batch Black-76 IV inversion for historical snapshots.

    This avoids the divide-by-zero warning in the live calibrator path by
    updating only the indices with meaningful vega.
    """
    discount = math.exp(-r * T)
    sqT = max(math.sqrt(T), 1e-9)
    intrinsic = np.maximum(0.0, discount * (F_arr - K_arr))
    upper = discount * F_arr
    valid = (
        np.isfinite(prices)
        & np.isfinite(F_arr)
        & np.isfinite(K_arr)
        & (prices > intrinsic + 1e-10)
        & (prices < upper - 1e-10)
    )

    sigma = np.full(len(prices), 0.5, dtype=float)
    for _ in range(n_iter):
        d1 = (np.log(F_arr / K_arr) + 0.5 * sigma**2 * T) / (sigma * sqT)
        d2 = d1 - sigma * sqT
        c = discount * (F_arr * norm.cdf(d1) - K_arr * norm.cdf(d2))
        vega = discount * F_arr * norm.pdf(d1) * sqT

        updatable = valid & np.isfinite(vega) & (vega > 1e-10) & np.isfinite(c)
        if not np.any(updatable):
            break

        step = np.zeros_like(sigma)
        step[updatable] = (c[updatable] - prices[updatable]) / vega[updatable]
        sigma = np.clip(sigma - step, 1e-6, 10.0)

    return np.where(valid, sigma, np.nan)


def _objective_historical(
    x: np.ndarray,
    points_by_expiry: dict[float, list[_CalPoint]],
    r: float,
    q: float,
    N: int,
) -> float:
    v0, kappa, theta, sigma_v, rho, lam, mu_j, sigma_j = x

    total = 0.0
    for T, pts in points_by_expiry.items():
        K_arr = np.array([p.K for p in pts])
        F_arr = np.array([p.F for p in pts])
        miv = np.array([p.market_iv for p in pts])
        w_arr = np.array([p.weight for p in pts])

        F_ref = float(np.median(F_arr))
        if not np.isfinite(F_ref) or F_ref <= 0.0:
            return 1e12
        S_expiry = F_ref * math.exp(-(r - q) * T)

        a, b, bma, k_arr, wrp = _cos_precompute(
            T, N, kappa, theta, sigma_v, rho, v0, r, q, lam, mu_j, sigma_j
        )
        prices = _vanilla_calls_batch(K_arr, S_expiry, a, b, bma, k_arr, wrp, r, T)
        iv_model = _bs_iv_batch_historical(prices, F_arr, K_arr, T, r)

        invalid = ~np.isfinite(iv_model)
        err_sq = np.where(invalid, 1.0, (iv_model - miv) ** 2)
        total += float(np.dot(w_arr, err_sq))

    return total


def calibrate_historical(
    chain_path: str,
    output_path: str,
    r: float = 0.05,
    q: float = 0.0,
    min_liquidity: float = 1.0,
    min_tte_days: float = 3.0,
    min_mark_price_btc: float = 0.001,
    min_iv_pct: float = 1.0,
    max_iv_pct: float = 300.0,
    mono_lo: float = 0.75,
    mono_hi: float = 1.35,
    max_per_expiry: int = 20,
    N_cos: int = 64,
    de_maxiter: int = 60,
    de_popsize: int = 6,
    polish_maxiter: int = 300,
) -> BatesParams:
    df = pd.read_csv(chain_path)
    print(f"Loaded {len(df)} instruments from {os.path.basename(chain_path)}")
    snapshot_ts = infer_snapshot_timestamp(df, chain_path)
    print(f"Chain snapshot timestamp: {snapshot_ts}")

    valid_underlying = pd.to_numeric(df["underlying_price"], errors="coerce")
    valid_tte = pd.to_numeric(df["tte_years"], errors="coerce")
    front = df[(valid_tte > 0) & (valid_underlying > 0)].sort_values("tte_years").iloc[0]
    S = float(front["underlying_price"]) * math.exp(-(r - q) * float(front["tte_years"]))
    print(f"Spot (derived from front-month forward): ${S:,.2f}")

    points = _build_historical_cal_set(
        df,
        min_liquidity=min_liquidity,
        min_tte_years=min_tte_days / 365.25,
        min_mark_price_btc=min_mark_price_btc,
        min_iv_pct=min_iv_pct,
        max_iv_pct=max_iv_pct,
        mono_lo=mono_lo,
        mono_hi=mono_hi,
        max_per_expiry=max_per_expiry,
    )
    points_by_expiry: dict[float, list[_CalPoint]] = {}
    for pt in points:
        key = round(pt.T, 6)
        points_by_expiry.setdefault(key, []).append(pt)

    print(f"Calibration universe: {len(points)} options across {len(points_by_expiry)} expiries")
    print(
        "Guardrails: "
        f"min_liquidity={min_liquidity}, min_tte_days={min_tte_days}, "
        f"min_mark_price_btc={min_mark_price_btc}, iv_range=[{min_iv_pct}, {max_iv_pct}]"
    )
    if len(points) < 5:
        raise RuntimeError(
            "Too few calibration points after filtering. "
            "Try lowering min_liquidity or widening the moneyness range."
        )

    atm_pts = sorted(points, key=lambda p: abs(math.log(p.K / p.F)))
    atm_iv = atm_pts[0].market_iv
    med_pts = [p for p in points if 30 / 365.25 < p.T < 120 / 365.25]
    theta0 = (min(med_pts, key=lambda p: abs(math.log(p.K / p.F))).market_iv ** 2) if med_pts else atm_iv ** 2

    bounds = [
        (1e-4, 4.0),
        (0.1, 20.0),
        (1e-4, 4.0),
        (0.01, 5.0),
        (-0.99, 0.99),
        (0.0, 20.0),
        (-0.5, 0.5),
        (0.01, 0.5),
    ]

    x0 = np.array([atm_iv ** 2, 2.0, theta0, 1.0, -0.3, 3.0, -0.02, 0.08])

    print(
        f"\nRunning differential evolution  (N_cos=32, {len(points)} pts, "
        f"maxiter={de_maxiter}, popsize={de_popsize})..."
    )
    de_result = differential_evolution(
        _objective_historical,
        bounds,
        args=(points_by_expiry, r, q, 32),
        seed=42,
        maxiter=de_maxiter,
        popsize=de_popsize,
        tol=1e-6,
        polish=False,
        workers=1,
    )
    print(f"  DE best SSE: {de_result.fun:.8f}")

    print(f"Running L-BFGS-B polish  (N_cos={N_cos})...")
    result = minimize(
        _objective_historical,
        de_result.x if np.isfinite(de_result.fun) else x0,
        args=(points_by_expiry, r, q, N_cos),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": polish_maxiter, "ftol": 1e-12, "gtol": 1e-8},
    )

    print(f"Converged: {result.success} | iterations: {result.nit} | final SSE: {result.fun:.8f}")
    if not result.success:
        raise RuntimeError(
            "Historical calibration did not converge; refusing to save parameters. "
            f"status={result.status} message={result.message!r}"
        )

    v0, kappa, theta, sigma_v, rho, lam, mu_j, sigma_j = result.x

    print("\nPer-expiry fit (RMSE in IV %):")
    for T_key in sorted(points_by_expiry):
        pts = points_by_expiry[T_key]
        K_arr = np.array([p.K for p in pts])
        F_arr = np.array([p.F for p in pts])
        miv = np.array([p.market_iv for p in pts])
        F_ref = float(np.median(F_arr))
        S_expiry = F_ref * math.exp(-(r - q) * T_key)

        a, b, bma, k_arr, wrp = _cos_precompute(
            T_key, N_cos, kappa, theta, sigma_v, rho, v0, r, q, lam, mu_j, sigma_j
        )
        prices = _vanilla_calls_batch(K_arr, S_expiry, a, b, bma, k_arr, wrp, r, T_key)
        iv_model = _bs_iv_batch_historical(prices, F_arr, K_arr, T_key, r)
        valid = ~np.isnan(iv_model)
        if valid.any():
            errs = (iv_model[valid] - miv[valid]) * 100
            rmse = math.sqrt(float(np.mean(errs**2)))
            bias = float(np.mean(errs))
            days = round(T_key * 365.25)
            print(f"  T≈{days:3d}d  n={valid.sum():2d}  RMSE={rmse:.2f}%  bias={bias:+.2f}%")

    params = BatesParams(
        S=S,
        r=r,
        q=q,
        v0=float(v0),
        kappa=float(kappa),
        theta=float(theta),
        sigma_v=float(sigma_v),
        rho=float(rho),
        lam=float(lam),
        mu_j=float(mu_j),
        sigma_j=float(sigma_j),
        calibration_source="historical-implied",
        calibrated_at=snapshot_ts,
    )
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    params.save(output_path)
    print(f"\n{params}")
    print(f"\nSaved → {output_path}")
    return params


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate Bates params from reconstructed historical chain snapshots")
    parser.add_argument("--chain", required=True, help="Path to reconstructed historical chain CSV")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--r", type=float, default=0.05, help="Risk-free rate")
    parser.add_argument("--min-liquidity", type=float, default=1.0, help="Min trade-based liquidity proxy")
    parser.add_argument("--min-tte", type=float, default=3.0, help="Min time to expiry in days")
    parser.add_argument("--min-mark-price-btc", type=float, default=0.001, help="Min option mark price in BTC")
    parser.add_argument("--min-iv-pct", type=float, default=1.0, help="Min IV percent")
    parser.add_argument("--max-iv-pct", type=float, default=300.0, help="Max IV percent")
    parser.add_argument("--de-maxiter", type=int, default=60, help="Differential evolution max iterations")
    parser.add_argument("--de-popsize", type=int, default=6, help="Differential evolution population size")
    parser.add_argument("--polish-maxiter", type=int, default=300, help="L-BFGS-B max iterations")
    parser.add_argument("--N", type=int, default=64, help="COS terms")
    args = parser.parse_args()

    calibrate_historical(
        chain_path=args.chain,
        output_path=args.output,
        r=args.r,
        min_liquidity=args.min_liquidity,
        min_tte_days=args.min_tte,
        min_mark_price_btc=args.min_mark_price_btc,
        min_iv_pct=args.min_iv_pct,
        max_iv_pct=args.max_iv_pct,
        N_cos=args.N,
        de_maxiter=args.de_maxiter,
        de_popsize=args.de_popsize,
        polish_maxiter=args.polish_maxiter,
    )


if __name__ == "__main__":
    main()
