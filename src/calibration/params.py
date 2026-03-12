"""
BatesParams dataclass — shared output type for all calibration sources.

Both calibration paths (implied.py and historical.py) produce a BatesParams
that can be passed directly to the C++ pricer via to_pricer_params().
"""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict


@dataclass
class BatesParams:
    # Spot / carry
    S: float          # current spot price (USD)
    r: float          # risk-free rate (annualised)
    q: float          # carry / dividend yield (annualised); 0 for BTC

    # Heston SV component
    v0: float         # initial instantaneous variance
    kappa: float      # mean-reversion speed
    theta: float      # long-run variance
    sigma_v: float    # vol of vol
    rho: float        # spot–variance correlation

    # Jump component
    lam: float        # jump intensity (jumps / year)
    mu_j: float       # mean log-jump size
    sigma_j: float    # std dev of log-jump size

    # Provenance
    calibration_source: str = "unknown"   # "implied" or "historical"
    calibrated_at: str = ""               # ISO 8601 UTC timestamp

    # ------------------------------------------------------------------ #
    def to_pricer_params(self):
        """Return a bates_pricer.Params object for the C++ pricer."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../pricer"))
        import bates_pricer as bp

        p = bp.Params()
        p.S       = self.S
        p.v0      = self.v0
        p.kappa   = self.kappa
        p.theta   = self.theta
        p.sigma_v = self.sigma_v
        p.rho     = self.rho
        p.lambda_ = self.lam
        p.mu_j    = self.mu_j
        p.sigma_j = self.sigma_j
        p.r       = self.r
        p.q       = self.q
        return p

    # ------------------------------------------------------------------ #
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: str) -> BatesParams:
        with open(path) as f:
            return cls(**json.load(f))

    # ------------------------------------------------------------------ #
    def __str__(self) -> str:
        atm_vol = self.v0 ** 0.5
        lr_vol  = self.theta ** 0.5
        return (
            f"BatesParams [{self.calibration_source}] @ {self.calibrated_at}\n"
            f"  S={self.S:,.2f}  r={self.r:.4f}  q={self.q:.4f}\n"
            f"  v0={self.v0:.6f} (σ₀={atm_vol:.2%})  "
            f"theta={self.theta:.6f} (σ_lr={lr_vol:.2%})\n"
            f"  kappa={self.kappa:.4f}  sigma_v={self.sigma_v:.4f}  rho={self.rho:.4f}\n"
            f"  lambda={self.lam:.4f}  mu_j={self.mu_j:.4f}  sigma_j={self.sigma_j:.4f}"
        )


import os  # noqa: E402  (needed by save())
