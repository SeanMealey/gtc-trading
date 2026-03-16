"""
Implied calibration of Bates SVJ parameters from the Deribit BTC options
IV surface (cross-sectional fit in IV space).

Algorithm
---------
1. Load data/deribit/btc_options_chain.csv (produced by get_deribit_options.py)
2. Filter to liquid, near-the-money call options with a valid mark_iv
3. For each candidate parameter vector, price vanilla calls under Bates via
   the COS method, then invert Black-76 to obtain model-implied vol
4. Minimise weighted SSE: Σ w_i * (σ_bates_i - σ_market_i)²
   weights w_i = sqrt(OI_i)  (dampens very large-OI contracts from dominating)
5. Save BatesParams to data/deribit/bates_params_implied.json

Notes
-----
- The Bates characteristic function is re-implemented here in vectorised numpy
  so the calibration is self-contained and does not require the C++ extension.
  The C++ pricer (bates_pricer.so) is used only for fast live pricing.
- Prices in the Deribit chain are in BTC; mark_iv is already in % (annualised),
  computed by Deribit from their own USD-denominated Black-76 model.
- For near-ATM short-dated options Δ(e^{-rT}) ≈ 0, so r has negligible impact;
  nonetheless we carry it through for consistency with the pricer.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize, differential_evolution
from scipy.stats import norm

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT      = os.path.abspath(os.path.join(_THIS_DIR, "../.."))
_CHAIN_CSV = os.path.join(_ROOT, "data/deribit/btc_options_chain.csv")
_OUT_PATH  = os.path.join(_ROOT, "data/deribit/bates_params_implied.json")
_CHAIN_FILE_RE = re.compile(r"btc_options_chain_(\d{8}_\d{6})\.csv$")

sys.path.insert(0, _THIS_DIR)   # allow  from params import BatesParams
from params import BatesParams  # noqa: E402


def infer_snapshot_timestamp(df: pd.DataFrame, chain_path: str) -> str:
    """
    Infer the snapshot timestamp for a chain file.

    Preference order:
      1. snapshot_ts column in the CSV
      2. timestamp embedded in the filename
      3. current UTC time as a last resort
    """
    if "snapshot_ts" in df.columns:
        non_null = df["snapshot_ts"].dropna()
        if not non_null.empty:
            return str(non_null.iloc[0])

    match = _CHAIN_FILE_RE.search(os.path.basename(chain_path))
    if match:
        dt = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# 1.  Bates characteristic function  (vectorised over COS frequency grid)
# ===========================================================================

def _bates_cf(u: np.ndarray, T: float,
              kappa: float, theta: float, sigma_v: float, rho: float, v0: float,
              r: float, q: float,
              lam: float, mu_j: float, sigma_j: float) -> np.ndarray:
    """
    Bates (1996) characteristic function of log(S(T)/S(0)).

    Mirrors the stable Albrecher et al. formulation in bates.hpp so that
    Python calibration and C++ live pricing use an identical model.

    Parameters
    ----------
    u : real-valued 1-D array (COS frequency grid  k*π/(b-a))
    T : time to expiry (years)
    Returns complex 1-D array of the same length as u.
    """
    j   = 1j
    u   = u.astype(complex)

    xi      = kappa - sigma_v * rho * u * j
    d       = np.sqrt(xi**2 + sigma_v**2 * u * (u + j))
    g2      = (xi - d) / (xi + d)
    exp_dT  = np.exp(-d * T)

    A = (xi - d) * T - 2.0 * np.log((1.0 - g2 * exp_dT) / (1.0 - g2))
    B = (xi - d) * (1.0 - exp_dT) / (sigma_v**2 * (1.0 - g2 * exp_dT))

    drift   = u * j * (r - q) * T
    heston  = np.exp(drift + (kappa * theta / sigma_v**2) * A + v0 * B)

    m_bar     = math.exp(mu_j + 0.5 * sigma_j**2) - 1.0
    jump_cf   = np.exp(j * u * mu_j - 0.5 * sigma_j**2 * u**2)
    jump_comp = np.exp(lam * T * (jump_cf - 1.0 - j * u * m_bar))

    return heston * jump_comp


# ===========================================================================
# 2.  COS truncation interval
# ===========================================================================

def _truncation_interval(T: float,
                         kappa, theta, sigma_v, v0,
                         r, q, lam, mu_j, sigma_j,
                         L: float = 12.0) -> tuple[float, float]:
    """
    [a, b] from the first four cumulants of log(S(T)/S(0)).
    Uses the deterministic-vol limit (exact when sigma_v=0, good approximation
    otherwise); mirrors the fallback path in cos_pricer.hpp.
    """
    if T <= 0.0:
        return -0.5, 0.5

    if abs(kappa) < 1e-12:
        iv = max(0.0, v0 * T)
    else:
        iv = theta * T + (v0 - theta) * (1.0 - math.exp(-kappa * T)) / kappa
        iv = max(0.0, iv)

    m_bar = math.exp(mu_j + 0.5 * sigma_j**2) - 1.0
    c1    = (r - q - lam * m_bar) * T - 0.5 * iv + lam * mu_j * T
    c2    = iv + lam * T * (mu_j**2 + sigma_j**2)
    c4    = lam * T * (mu_j**4 + 6.0 * mu_j**2 * sigma_j**2 + 3.0 * sigma_j**4)

    width = L * math.sqrt(abs(c2) + math.sqrt(abs(c4)))
    return c1 - width, c1 + width


# ===========================================================================
# 3.  COS vanilla call pricer  (scalar + batch)
# ===========================================================================

def _cos_precompute(T: float, N: int,
                    kappa, theta, sigma_v, rho, v0,
                    r, q, lam, mu_j, sigma_j) -> tuple:
    """
    Pre-compute per-expiry COS quantities that are shared across all strikes.

    Returns (a, b, bma, k_arr, weighted_re_phi) where:
      weighted_re_phi[k] = w_k * Re[φ(kπ/(b-a)) * exp(-ikπa/(b-a))]
    Calling this once per expiry then reusing for many strikes is the key
    speedup in the calibration objective.
    """
    a, b = _truncation_interval(T, kappa, theta, sigma_v, v0,
                                r, q, lam, mu_j, sigma_j)
    bma   = b - a
    k_arr = np.arange(N, dtype=float)
    u_arr = k_arr * math.pi / bma
    phi   = _bates_cf(u_arr, T, kappa, theta, sigma_v, rho, v0,
                      r, q, lam, mu_j, sigma_j)
    re    = np.real(phi * np.exp(-1j * u_arr * a))
    w     = np.ones(N);  w[0] = 0.5
    return a, b, bma, k_arr, w * re


def _payoff_V_batch(K_arr: np.ndarray, S: float,
                    a: float, b: float, bma: float,
                    k_arr: np.ndarray) -> np.ndarray:
    """
    Compute payoff coefficient matrix V[n, N] = χ[n,N] - (K/S)[n,1] * ψ[n,N]
    for n strikes simultaneously.

    x0[n] = max(log(K[n]/S), a)   — payoff boundary per option
    """
    x0 = np.maximum(np.log(K_arr / S), a)   # (n,)
    alpha = k_arr * math.pi / bma            # (N,)

    # --- χ  (n x N) ---
    # k=0: exp(b) - exp(x0)
    # k≥1: [cos(α(b-a))·e^b - cos(α(x0-a))·e^x0 + α·(sin(α(b-a))·e^b - sin(α(x0-a))·e^x0)] / (1+α²)
    exp_b  = math.exp(b)
    ex0    = np.exp(x0)                           # (n,)
    a2     = 1.0 + alpha**2                       # (N,)
    cosb   = np.cos(alpha * (b - a))              # (N,)  = cos(kπ) = ±1
    sinb   = np.sin(alpha * (b - a))              # (N,)  = sin(kπ) = 0 (integer k)

    # (n, N) broadcasting
    cosx0  = np.cos(alpha[None, :] * (x0[:, None] - a))   # (n, N)
    sinx0  = np.sin(alpha[None, :] * (x0[:, None] - a))   # (n, N)

    chi = np.where(
        k_arr[None, :] == 0,
        exp_b - ex0[:, None],
        (cosb[None, :] * exp_b
         - cosx0 * ex0[:, None]
         + alpha[None, :] * (sinb[None, :] * exp_b - sinx0 * ex0[:, None])
        ) / a2[None, :]
    )  # (n, N)

    # --- ψ  (n x N) ---
    # k=0: b - x0
    # k≥1: (bma/(kπ)) * (sin(α(b-a)) - sin(α(x0-a)))  = -bma/(kπ) * sin(α(x0-a))
    with np.errstate(divide='ignore', invalid='ignore'):
        psi = np.where(
            k_arr[None, :] == 0,
            b - x0[:, None],
            (bma / (k_arr[None, :] * math.pi))
            * (sinb[None, :] - sinx0)
        )  # (n, N)

    V = chi - (K_arr / S)[:, None] * psi          # (n, N)
    V[x0 >= b, :] = 0.0                           # deep OTM: zero payoff
    return V


def _vanilla_calls_batch(K_arr: np.ndarray, S: float,
                         a, b, bma, k_arr, weighted_re_phi,
                         r: float, T: float) -> np.ndarray:
    """
    Price vanilla calls for an array of strikes at the same expiry,
    reusing precomputed COS factors (CF evaluated once for all strikes).
    """
    V      = _payoff_V_batch(K_arr, S, a, b, bma, k_arr)   # (n, N)
    prices = math.exp(-r * T) * S * (2.0 / bma) * (V @ weighted_re_phi)
    return np.maximum(prices, 0.0)


def vanilla_call_cos(K: float, T: float, S: float,
                     kappa, theta, sigma_v, rho, v0,
                     r, q, lam, mu_j, sigma_j,
                     N: int = 64) -> float:
    """
    Discounted vanilla (European) call price via the COS method  (scalar API).

    C = e^{-rT} * S * (2/(b-a)) * Σ_{k=0}^{N-1}' Re[φ_k·e^{-ikπa/(b-a)}] · V_k
    V_k = χ_k(x0, b) - (K/S) · ψ_k(x0, b),   x0 = max(log(K/S), a)

    Returns 0.0 if T ≤ 0 or the option is beyond the truncation domain.
    """
    if T <= 0.0:
        return max(0.0, S - K)
    if K <= 0.0:
        return S * math.exp(-q * T)

    a, b, bma, k_arr, wrp = _cos_precompute(T, N, kappa, theta, sigma_v, rho, v0,
                                             r, q, lam, mu_j, sigma_j)
    prices = _vanilla_calls_batch(np.array([K]), S, a, b, bma, k_arr, wrp, r, T)
    return float(prices[0])


# ===========================================================================
# 4.  Black-76 (forward Black) pricer and IV inversion
# ===========================================================================

def _bs76_call(F: float, K: float, T: float, sigma: float, r: float) -> float:
    """Discounted Black-76 call: C = e^{-rT} * [F*N(d1) - K*N(d2)]."""
    if T <= 0.0 or sigma <= 0.0:
        return max(0.0, math.exp(-r * T) * (F - K))
    sqT  = math.sqrt(T)
    d1   = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqT)
    d2   = d1 - sigma * sqT
    return math.exp(-r * T) * (F * norm.cdf(d1) - K * norm.cdf(d2))


def _bs76_vega(F: float, K: float, T: float, sigma: float, r: float) -> float:
    """Vega of Black-76 call: ∂C/∂σ = e^{-rT} * F * N'(d1) * sqrt(T)."""
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    sqT = math.sqrt(T)
    d1  = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqT)
    return math.exp(-r * T) * F * norm.pdf(d1) * sqT


def _bs_implied_vol(price: float, F: float, K: float, T: float, r: float,
                    tol: float = 1e-8, max_iter: int = 50) -> float:
    """
    Invert Black-76 for implied vol  (scalar, used for live signal computation).
    Returns NaN if inversion fails or price is outside no-arbitrage bounds.
    """
    intrinsic = max(0.0, math.exp(-r * T) * (F - K))
    upper     = math.exp(-r * T) * F

    if price <= intrinsic or price >= upper:
        return float("nan")

    sigma = 0.5
    for _ in range(max_iter):
        c     = _bs76_call(F, K, T, sigma, r)
        vega  = _bs76_vega(F, K, T, sigma, r)
        if abs(vega) < 1e-14:
            return float("nan")
        sigma -= (c - price) / vega
        sigma  = max(1e-6, min(sigma, 20.0))
        if abs(c - price) < tol:
            break
    return sigma


def _bs_iv_batch(prices: np.ndarray, F_arr: np.ndarray, K_arr: np.ndarray,
                 T: float, r: float, n_iter: int = 20) -> np.ndarray:
    """
    Vectorised Newton-Raphson Black-76 IV inversion for an array of options.

    Fixed n_iter iterations (no early stopping); 20 is sufficient for 1e-8 tol.
    Options outside no-arbitrage bounds receive NaN.
    """
    discount  = math.exp(-r * T)
    sqT       = max(math.sqrt(T), 1e-9)
    intrinsic = np.maximum(0.0, discount * (F_arr - K_arr))
    valid     = (prices > intrinsic + 1e-12) & (prices < discount * F_arr - 1e-12)

    sigma = np.full(len(prices), 0.5, dtype=float)

    for _ in range(n_iter):
        d1   = (np.log(F_arr / K_arr) + 0.5 * sigma**2 * T) / (sigma * sqT)
        d2   = d1 - sigma * sqT
        c    = discount * (F_arr * norm.cdf(d1) - K_arr * norm.cdf(d2))
        vega = discount * F_arr * norm.pdf(d1) * sqT
        step = np.zeros_like(sigma)
        updatable = valid & (vega > 1e-14) & np.isfinite(c) & np.isfinite(vega)
        step[updatable] = (c[updatable] - prices[updatable]) / vega[updatable]
        sigma = np.clip(sigma - step, 1e-6, 20.0)
        if updatable.any() and np.max(np.abs(c[updatable] - prices[updatable])) < 1e-8:
            break

    return np.where(valid, sigma, np.nan)


# ===========================================================================
# 5.  Bates implied vol (Bates price → Black-76 IV)
# ===========================================================================

def bates_iv(K: float, T: float, F: float, S: float,
             kappa, theta, sigma_v, rho, v0,
             r, q, lam, mu_j, sigma_j,
             N: int = 64) -> float:
    """
    Compute the Black-76 implied vol that matches the Bates vanilla call price.
    Returns NaN if the inversion fails.
    """
    price = vanilla_call_cos(K, T, S, kappa, theta, sigma_v, rho, v0,
                             r, q, lam, mu_j, sigma_j, N)
    return _bs_implied_vol(price, F, K, T, r)


# ===========================================================================
# 6.  Build calibration set
# ===========================================================================

@dataclass
class _CalPoint:
    K:           float   # strike (USD)
    T:           float   # time to expiry (years)
    F:           float   # forward price (USD) = underlying_price from Deribit
    market_iv:   float   # Deribit mark_iv in decimal (e.g. 0.50 = 50%)
    weight:      float   # sqrt(OI) weighting


def _build_cal_set(df: pd.DataFrame,
                   min_oi: float = 10.0,
                   min_tte_years: float = 3.0 / 365.25,
                   mono_lo: float = 0.75,
                   mono_hi: float = 1.35,
                   max_per_expiry: int = 20) -> list[_CalPoint]:
    """
    Filter the Deribit chain to a calibration universe of liquid near-money calls.

    Filters applied (in order):
      1. Calls only (puts carry same info via put-call parity)
      2. mark_iv present and positive
      3. open_interest >= min_oi
      4. tte_years >= min_tte_years
      5. moneyness  mono_lo <= K/F <= mono_hi
      6. At most max_per_expiry options per expiry, keeping highest-OI ones
    """
    df = df[df["option_type"] == "call"].copy()
    df = df[df["mark_iv"].notna() & (df["mark_iv"] > 0)]
    df = df[df["open_interest"] >= min_oi]
    df = df[df["tte_years"] >= min_tte_years]
    df = df[df["underlying_price"].notna() & (df["underlying_price"] > 0)]

    df["moneyness"] = df["strike"] / df["underlying_price"]
    df = df[(df["moneyness"] >= mono_lo) & (df["moneyness"] <= mono_hi)]

    # Cap per expiry
    df = (df.sort_values("open_interest", ascending=False)
            .groupby("expiry_dt", sort=False)
            .head(max_per_expiry)
            .reset_index(drop=True))

    points = []
    for _, row in df.iterrows():
        points.append(_CalPoint(
            K          = float(row["strike"]),
            T          = float(row["tte_years"]),
            F          = float(row["underlying_price"]),
            market_iv  = float(row["mark_iv"]) / 100.0,   # % → decimal
            weight     = math.sqrt(max(float(row["open_interest"]), 1.0)),
        ))

    return points


# ===========================================================================
# 7.  Objective function  (batched by expiry for speed)
# ===========================================================================

def _objective(x: np.ndarray,
               points_by_expiry: dict,   # {T: list[_CalPoint]}
               r: float, q: float, N: int) -> float:
    """
    Weighted SSE in IV space, batched by expiry.

    Per-expiry the CF is evaluated once over N frequencies; payoff coefficients
    and BS inversion are fully vectorised over strikes.  This is ~50× faster
    than calling bates_iv() per option individually.

    Parameter vector x = [v0, kappa, theta, sigma_v, rho, lam, mu_j, sigma_j]
    """
    v0, kappa, theta, sigma_v, rho, lam, mu_j, sigma_j = x

    total = 0.0
    for T, pts in points_by_expiry.items():
        K_arr  = np.array([p.K          for p in pts])
        F_arr  = np.array([p.F          for p in pts])
        miv    = np.array([p.market_iv  for p in pts])
        w_arr  = np.array([p.weight     for p in pts])
        # Use the expiry's own forward level to infer a spot anchor for pricing.
        # This keeps the COS call price and Black-76 IV inversion on the same curve.
        F_ref = float(np.median(F_arr))
        if not np.isfinite(F_ref) or F_ref <= 0.0:
            return 1e12
        S_expiry = F_ref * math.exp(-(r - q) * T)

        # Pre-compute CF + COS factors once for this expiry
        a, b, bma, k_arr, wrp = _cos_precompute(
            T, N, kappa, theta, sigma_v, rho, v0, r, q, lam, mu_j, sigma_j
        )
        prices   = _vanilla_calls_batch(K_arr, S_expiry, a, b, bma, k_arr, wrp, r, T)
        if not np.all(np.isfinite(prices)):
            return 1e12
        iv_model = _bs_iv_batch(prices, F_arr, K_arr, T, r)
        valid_iv = np.isfinite(iv_model)
        invalid_count = int((~valid_iv).sum())
        if invalid_count > max(2, len(pts) // 4):
            return 1e12 + float(np.sum(w_arr[~valid_iv]) * 25.0)

        err_sq = np.empty_like(miv)
        err_sq[valid_iv] = (iv_model[valid_iv] - miv[valid_iv]) ** 2
        err_sq[~valid_iv] = 25.0
        total += float(np.dot(w_arr, err_sq))

    return total


def _score_candidate(x: np.ndarray,
                     points_by_expiry: dict,
                     r: float,
                     q: float,
                     N: int) -> float:
    """Safely score a parameter vector, returning +inf if evaluation fails."""
    try:
        score = float(_objective(x, points_by_expiry, r, q, N))
    except (ArithmeticError, FloatingPointError, OverflowError, ValueError):
        return float("inf")
    return score if math.isfinite(score) else float("inf")


def _load_saved_candidate(output_path: str) -> np.ndarray | None:
    """Load a previously saved Bates parameter vector if present and valid."""
    if not os.path.exists(output_path):
        return None

    try:
        with open(output_path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    keys = ("v0", "kappa", "theta", "sigma_v", "rho", "lam", "mu_j", "sigma_j")
    if any(key not in raw for key in keys):
        return None

    x = np.array([raw[key] for key in keys], dtype=float)
    if not np.all(np.isfinite(x)):
        return None
    return x


# ===========================================================================
# 8.  Main calibration entry point
# ===========================================================================

def calibrate(
    chain_path: str = _CHAIN_CSV,
    r: float = 0.05,
    q: float = 0.0,
    min_oi: float = 10.0,
    min_tte_days: float = 3.0,
    mono_lo: float = 0.75,
    mono_hi: float = 1.35,
    max_per_expiry: int = 20,
    N_cos: int = 64,
    de_N_cos: int | None = None,
    output_path: str = _OUT_PATH,
) -> BatesParams:
    """
    Calibrate Bates SVJ parameters to the Deribit BTC IV surface.

    Parameters
    ----------
    chain_path      Path to btc_options_chain.csv (default: data/deribit/latest)
    r               USD risk-free rate (annualised)
    q               Carry / dividend yield (0 for BTC)
    min_oi          Minimum open interest per contract (BTC)
    min_tte_days    Minimum time to expiry to include
    mono_lo/hi      K/F moneyness range
    max_per_expiry  Max options per expiry in calibration set
    N_cos           COS terms for vanilla call pricing (64 → ~5-digit accuracy)
    de_N_cos        COS terms for the DE search (defaults to N_cos)
    output_path     Where to save BatesParams JSON

    Returns
    -------
    BatesParams  (calibration_source = "implied")
    """
    if N_cos <= 0:
        raise ValueError("N_cos must be positive")
    if de_N_cos is None:
        de_N_cos = N_cos
    if de_N_cos <= 0:
        raise ValueError("de_N_cos must be positive")

    # ------------------------------------------------------------------ #
    # Load chain
    # ------------------------------------------------------------------ #
    df = pd.read_csv(chain_path)
    print(f"Loaded {len(df)} instruments from {os.path.basename(chain_path)}")
    snapshot_ts = infer_snapshot_timestamp(df, chain_path)
    print(f"Chain snapshot timestamp: {snapshot_ts}")

    # Derive S from front-month forward (approximately spot for short T)
    front = df[df["tte_years"] > 0].sort_values("tte_years").iloc[0]
    S = float(front["underlying_price"]) * math.exp(
        -(r - q) * float(front["tte_years"])
    )
    print(f"Spot (derived from front-month forward): ${S:,.2f}")

    # ------------------------------------------------------------------ #
    # Build calibration set
    # ------------------------------------------------------------------ #
    points = _build_cal_set(
        df,
        min_oi=min_oi,
        min_tte_years=min_tte_days / 365.25,
        mono_lo=mono_lo,
        mono_hi=mono_hi,
        max_per_expiry=max_per_expiry,
    )
    # Group by expiry for batched objective evaluation
    points_by_expiry: dict[float, list[_CalPoint]] = {}
    for pt in points:
        key = round(pt.T, 6)
        points_by_expiry.setdefault(key, []).append(pt)

    print(f"Calibration universe: {len(points)} options across "
          f"{len(points_by_expiry)} expiries")

    if len(points) < 5:
        raise RuntimeError(
            "Too few calibration points after filtering. "
            "Try lowering min_oi or widening the moneyness range."
        )

    # ------------------------------------------------------------------ #
    # Initial parameter guess
    # ------------------------------------------------------------------ #
    # v0: use near-ATM IV of nearest expiry
    nearest = min(points, key=lambda p: p.T)
    atm_pts = sorted(points, key=lambda p: abs(math.log(p.K / p.F)))
    atm_iv  = atm_pts[0].market_iv

    # theta: use near-ATM IV of a medium-term expiry (30-90 days)
    med_pts = [p for p in points if 30/365.25 < p.T < 120/365.25]
    if med_pts:
        atm_med = min(med_pts, key=lambda p: abs(math.log(p.K / p.F)))
        theta0  = atm_med.market_iv ** 2
    else:
        theta0  = atm_iv ** 2

    x0 = np.array([
        atm_iv ** 2,   # v0
        2.0,           # kappa
        theta0,        # theta
        1.0,           # sigma_v
        -0.3,          # rho
        3.0,           # lam
        -0.02,         # mu_j
        0.08,          # sigma_j
    ])

    bounds = [
        (1e-4, 4.0),    # v0
        (0.1,  20.0),   # kappa
        (1e-4, 4.0),    # theta
        (0.01, 5.0),    # sigma_v
        (-0.99, 0.99),  # rho
        (0.0,  20.0),   # lam
        (-0.5, 0.5),    # mu_j
        (0.01, 0.5),    # sigma_j
    ]

    saved_x = _load_saved_candidate(output_path)
    candidate_pool: list[tuple[str, np.ndarray]] = [("initial guess", x0)]
    if saved_x is not None:
        candidate_pool.append(("saved params", saved_x))

    # ------------------------------------------------------------------ #
    # Optimise  (global DE pass → local L-BFGS-B polish)
    # ------------------------------------------------------------------ #
    # Differential evolution avoids the Bates degeneracy (λ→∞, σ_j→0) that
    # local gradient methods fall into when the jump component is weakly
    # identified. We score all downstream candidates at full resolution before
    # the local polish step so the optimizer starts from the most trustworthy
    # point we have.
    print(f"\nRunning differential evolution  (N_cos={de_N_cos}, {len(points)} pts)...")
    t0 = time.perf_counter()
    de_result = differential_evolution(
        _objective,
        bounds,
        args=(points_by_expiry, r, q, de_N_cos),
        seed=42,
        maxiter=200,
        popsize=8,
        tol=1e-6,
        polish=False,
        workers=1,
    )
    print(f"  DE best SSE: {de_result.fun:.8f}  ({time.perf_counter()-t0:.1f}s)")
    de_full_sse = _score_candidate(de_result.x, points_by_expiry, r, q, N_cos)
    if de_N_cos != N_cos:
        print(f"  DE candidate rescored at N_cos={N_cos}: {de_full_sse:.8f}")
    candidate_pool.append(("DE candidate", de_result.x.copy()))

    scored_candidates: list[tuple[float, str, np.ndarray]] = []
    print("Full-resolution candidate SSEs:")
    for label, x in candidate_pool:
        score = _score_candidate(x, points_by_expiry, r, q, N_cos)
        if math.isfinite(score):
            scored_candidates.append((score, label, x.copy()))
            print(f"  {label:>13}: {score:.8f}")
        else:
            print(f"  {label:>13}: inf")

    if not scored_candidates:
        raise RuntimeError("Calibration failed before polish; no finite candidate found.")

    best_start_score, best_start_label, best_start_x = min(
        scored_candidates, key=lambda item: item[0]
    )
    print(f"Polish start: {best_start_label}  (SSE={best_start_score:.8f})")

    print(f"Running L-BFGS-B polish  (N_cos={N_cos})...")
    t1 = time.perf_counter()
    result = minimize(
        _objective,
        best_start_x,
        args=(points_by_expiry, r, q, N_cos),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 500, "ftol": 1e-12, "gtol": 1e-8},
    )

    elapsed = time.perf_counter() - t1
    print(f"Converged: {result.success}  |  iterations: {result.nit}  |  "
          f"polish time: {elapsed:.1f}s  |  total: {time.perf_counter()-t0:.1f}s")
    print(f"Final SSE: {result.fun:.8f}")
    polish_score = _score_candidate(result.x, points_by_expiry, r, q, N_cos)
    if math.isfinite(polish_score):
        scored_candidates.append((polish_score, "polish result", result.x.copy()))

    best_score, best_label, best_x = min(scored_candidates, key=lambda item: item[0])
    if not math.isfinite(best_score) or best_score >= 1e11:
        raise RuntimeError(
            "Calibration did not find a usable full-resolution candidate. "
            f"polish_status={result.status} message={result.message!r}"
        )

    if not result.success:
        print(
            "Polish exited early; retaining best full-resolution candidate "
            f"from {best_label}  (SSE={best_score:.8f}, status={result.status})"
        )
    elif best_label != "polish result":
        print(
            "Polish converged but did not improve the full-resolution fit; "
            f"retaining {best_label}  (SSE={best_score:.8f})"
        )

    v0, kappa, theta, sigma_v, rho, lam, mu_j, sigma_j = best_x

    # ------------------------------------------------------------------ #
    # Fit diagnostics
    # ------------------------------------------------------------------ #
    print("\nPer-expiry fit (RMSE in IV %):")
    for T_key in sorted(points_by_expiry):
        pts = points_by_expiry[T_key]
        K_arr = np.array([p.K for p in pts])
        F_arr = np.array([p.F for p in pts])
        miv   = np.array([p.market_iv for p in pts])
        F_ref = float(np.median(F_arr))
        S_expiry = F_ref * math.exp(-(r - q) * T_key)

        a, b, bma, k_arr, wrp = _cos_precompute(
            T_key, N_cos, kappa, theta, sigma_v, rho, v0, r, q, lam, mu_j, sigma_j
        )
        prices   = _vanilla_calls_batch(K_arr, S_expiry, a, b, bma, k_arr, wrp, r, T_key)
        iv_model = _bs_iv_batch(prices, F_arr, K_arr, T_key, r)

        valid = ~np.isnan(iv_model)
        if valid.any():
            errs = (iv_model[valid] - miv[valid]) * 100
            rmse = math.sqrt(float(np.mean(errs**2)))
            bias = float(np.mean(errs))
            days = round(T_key * 365.25)
            print(f"  T≈{days:3d}d  n={valid.sum():2d}  "
                  f"RMSE={rmse:.2f}%  bias={bias:+.2f}%")

    # ------------------------------------------------------------------ #
    # Assemble and save
    # ------------------------------------------------------------------ #
    params = BatesParams(
        S       = S,
        r       = r,
        q       = q,
        v0      = float(v0),
        kappa   = float(kappa),
        theta   = float(theta),
        sigma_v = float(sigma_v),
        rho     = float(rho),
        lam     = float(lam),
        mu_j    = float(mu_j),
        sigma_j = float(sigma_j),
        calibration_source = "implied",
        calibrated_at      = snapshot_ts,
    )

    params.save(output_path)
    print(f"\n{params}")
    print(f"\nSaved → {output_path}")
    return params


# ===========================================================================
# CLI entry point
# ===========================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Calibrate Bates params to Deribit IV surface")
    parser.add_argument("--chain",    default=_CHAIN_CSV,  help="Path to btc_options_chain.csv")
    parser.add_argument("--output",   default=_OUT_PATH,   help="Output JSON path")
    parser.add_argument("--r",        type=float, default=0.05,  help="Risk-free rate")
    parser.add_argument("--min-oi",   type=float, default=10.0,  help="Min open interest (BTC)")
    parser.add_argument("--min-tte",  type=float, default=3.0,   help="Min time to expiry (days)")
    parser.add_argument("--N",        type=int,   default=64,    help="COS terms")
    parser.add_argument("--de-N",     type=int,   default=None,  help="COS terms for DE search")
    args = parser.parse_args()

    calibrate(
        chain_path    = args.chain,
        r             = args.r,
        min_oi        = args.min_oi,
        min_tte_days  = args.min_tte,
        N_cos         = args.N,
        de_N_cos      = args.de_N,
        output_path   = args.output,
    )
