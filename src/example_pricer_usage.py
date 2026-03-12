import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "pricer"))

import bates_pricer as bp


def main() -> None:
    params = bp.Params()
    params.S = 100000.0
    params.v0 = 0.45 * 0.45
    params.kappa = 2.0
    params.theta = 0.40 * 0.40
    params.sigma_v = 0.8
    params.rho = -0.55
    params.lambda_ = 4.0
    params.mu_j = -0.04
    params.sigma_j = 0.20
    params.r = 0.04
    params.q = 0.0

    strike = 105000.0
    expiry_days = 7.0
    T = expiry_days / 365.25

    call_price = bp.binary_call(strike, T, params, N=256)
    put_price = bp.binary_put(strike, T, params, N=256)
    call_prob = bp.binary_call_prob(strike, T, params, N=256)
    greeks = bp.greeks(strike, T, params, N=256)
    mc = bp.binary_call_mc(strike, T, params, n_paths=100000, n_steps=400, seed=42)

    print("Bates digital option example")
    print(f"spot: {params.S:,.2f}")
    print(f"strike: {strike:,.2f}")
    print(f"expiry_days: {expiry_days:.1f}")
    print(f"time_to_expiry_years: {T:.8f}")
    print()
    print(f"binary_call_price: {call_price:.8f}")
    print(f"binary_put_price:  {put_price:.8f}")
    print(f"call_probability:  {call_prob:.8f}")
    print(f"discount_factor:   {math.exp(-params.r * T):.8f}")
    print()
    print("Greeks")
    print(f"delta:        {greeks.delta:.8f}")
    print(f"vega:         {greeks.vega:.8f}")
    print(f"theta:        {greeks.theta:.8f}")
    print(f"lambda_sens:  {greeks.lambda_sens:.8f}")
    print()
    print("Monte Carlo cross-check")
    print(f"mc_price:     {mc.price:.8f}")
    print(f"mc_stderr:    {mc.stderr:.8f}")
    print(f"mc_paths:     {mc.paths}")


if __name__ == "__main__":
    main()
