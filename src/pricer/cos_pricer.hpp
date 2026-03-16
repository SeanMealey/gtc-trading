#pragma once
#include <vector>
#include <cmath>
#include <complex>
#include <stdexcept>
#include "bates.hpp"

/**
 * COS Method (Fang & Oosterlee, 2008) for binary option pricing.
 *
 * Binary call pays $1 if S(T) > K:
 *   C_bin = e^{-rT} * Q(S(T) > K)
 *   Q(S(T) > K) = 1/2 + (1/π) ∫₀^∞ Re[e^{-iu·ln(K/S)} φ(u) / (iu)] du
 *
 * COS approximates this via a cosine series on [a, b] derived from cumulants.
 */

namespace cos_pricer {

using cd = std::complex<double>;

/**
 * Compute the first four cumulants of log(S(T)/S(0)) under the Bates model.
 * Used to set the truncation interval [a, b].
 */
struct Cumulants { double c1, c2, c4; };

inline double log_mgf(double t, double T, const bates::Params& p) {
    if (t == 0.0) return 0.0;
    cd phi = bates::characteristic_function(cd(0.0, -t), T, p);
    return std::real(std::log(phi));
}

inline double deterministic_integrated_variance(double T, const bates::Params& p) {
    if (T <= 0.0) return 0.0;
    if (std::abs(p.kappa) < 1e-12) return std::max(0.0, p.v0 * T);
    double iv = p.theta * T + (p.v0 - p.theta) * (1.0 - std::exp(-p.kappa * T)) / p.kappa;
    return std::max(0.0, iv);
}

inline Cumulants deterministic_limit_cumulants(double T, const bates::Params& p) {
    double iv = deterministic_integrated_variance(T, p);
    double m_bar = std::exp(p.mu_j + 0.5 * p.sigma_j * p.sigma_j) - 1.0;

    double c1 = (p.r - p.q - p.lambda * m_bar) * T
                - 0.5 * iv
                + p.lambda * p.mu_j * T;
    double c2 = iv + p.lambda * T * (p.mu_j * p.mu_j + p.sigma_j * p.sigma_j);
    double c4 = p.lambda * T
                * (p.mu_j * p.mu_j * p.mu_j * p.mu_j
                   + 6.0 * p.mu_j * p.mu_j * p.sigma_j * p.sigma_j
                   + 3.0 * p.sigma_j * p.sigma_j * p.sigma_j * p.sigma_j);

    return {c1, std::max(c2, 0.0), std::max(c4, 0.0)};
}

inline Cumulants bates_cumulants(double T, const bates::Params& p) {
    // Derive cumulants from the actual Bates log-MGF K(t) = log E[e^{tX}],
    // evaluated via the characteristic function at u = -it. This keeps the
    // COS truncation interval aligned with the full Heston+jump dynamics.
    //
    // Near the deterministic-vol limit, the numerical stencil can become
    // ill-conditioned and collapse c2/c4. In that regime, fall back to the
    // deterministic integrated-variance limit, which is exact when sigma_v=0
    // and a good approximation when sigma_v is very small.
    const double h = 1e-3;
    const double f0 = 0.0;
    const double fp1 = log_mgf(h, T, p);
    const double fm1 = log_mgf(-h, T, p);
    const double fp2 = log_mgf(2.0 * h, T, p);
    const double fm2 = log_mgf(-2.0 * h, T, p);

    double c1 = (fm2 - 8.0 * fm1 + 8.0 * fp1 - fp2) / (12.0 * h);
    double c2 = (-fm2 + 16.0 * fm1 - 30.0 * f0 + 16.0 * fp1 - fp2)
                / (12.0 * h * h);
    double c4 = (fm2 - 4.0 * fm1 + 6.0 * f0 - 4.0 * fp1 + fp2)
                / std::pow(h, 4);

    Cumulants numerical{c1, std::max(c2, 0.0), std::max(c4, 0.0)};
    Cumulants deterministic = deterministic_limit_cumulants(T, p);

    bool near_constant_vol = std::abs(p.sigma_v) <= 1e-3;
    bool collapsed_variance =
        deterministic.c2 > 0.0 && numerical.c2 < 0.25 * deterministic.c2;

    if (near_constant_vol || collapsed_variance) {
        return deterministic;
    }
    return numerical;
}

/**
 * Truncation interval [a, b] for the log-price domain.
 * L controls the tail coverage (L=12 gives ~6σ, sufficient for 6+ digit accuracy).
 */
inline std::pair<double, double> truncation_interval(
        double T, const bates::Params& p, double L = 12.0) {
    auto [c1, c2, c4] = bates_cumulants(T, p);
    double width = L * std::sqrt(std::abs(c2) + std::sqrt(std::abs(c4)));
    return {c1 - width, c1 + width};
}

/**
 * COS coefficients for the binary call payoff indicator 1_{x > ln(K/S)}.
 *
 * V_k = (2 / (b-a)) * ∫_{ln(K/S)}^{b} cos(k*π*(x-a)/(b-a)) dx
 *
 * Analytic result:
 *   V_0 = (b - ln(K/S)) / (b - a)        [k=0 term, no cosine]
 *   V_k = [sin(kπ(b-a_)/(b-a)) - sin(kπ(ln(K/S)-a)/(b-a))] / (kπ)  * 2/(b-a) * (b-a)
 *       = (2/(kπ)) * [sin(kπ*(b - x0)/(b-a)) - sin(kπ*(x0 - a)/(b-a))] ... simplified below
 */
inline std::vector<double> payoff_coefficients(
        double K, double S, double a, double b, int N) {
    std::vector<double> V(N);
    double x0   = std::log(K / S);  // log(K/S), the payoff boundary in log-space
    double bma  = b - a;

    if (x0 >= b) {
        // S(T) can never exceed K in the truncated domain: all zeros
        return V;
    }

    double lo = std::max(x0, a);  // integration lower bound (clipped to [a,b])

    // k=0: V_0 = (2/(b-a)) * ∫_{lo}^{b} 1 dx = 2*(b-lo)/(b-a)
    // The 1/2 weight is applied in the summation loop (standard COS convention).
    V[0] = 2.0 * (b - lo) / bma;

    // k>=1: analytic integral of cos(kπ(x-a)/(b-a)) over [lo, b]
    for (int k = 1; k < N; ++k) {
        double kpi_bma = k * M_PI / bma;
        // 2/(b-a) * (b-a)/(kπ) * [sin(kπ(b-a)/(b-a)) - sin(kπ(lo-a)/(b-a))]
        // = (2/(kπ)) * [sin(kπ) - sin(kπ(lo-a)/(b-a))]
        // sin(kπ) = 0 always, so:
        V[k] = (2.0 / (k * M_PI))
               * (-std::sin(kpi_bma * (lo - a)));
        // Equivalently: (2/(kπ)) * sin(kπ(b-lo)/(b-a)) * ... let's use the clean form:
        // ∫_{lo}^{b} cos(kπ(x-a)/(b-a)) dx = (b-a)/(kπ) * [sin(kπ(b-a)/(b-a)) - sin(kπ(lo-a)/(b-a))]
        //                                    = (b-a)/(kπ) * [0 - sin(kπ(lo-a)/(b-a))]
        // Then multiply by 2/(b-a): gives -(2/(kπ)) * sin(kπ(lo-a)/(b-a))  ✓
    }
    return V;
}

/**
 * Price a cash-or-nothing binary call using the COS method.
 *
 * @param K     Strike price
 * @param T     Time to expiry (years)
 * @param p     Bates model parameters
 * @param N     Number of COS terms (64-128 gives ~6 digit accuracy)
 * @return      Undiscounted risk-neutral probability Q(S(T) > K),
 *              multiply by e^{-rT} for present value.
 */
inline double binary_call_prob(
        double K, double T, const bates::Params& p, int N = 256) {
    if (T <= 0.0) return (p.S > K) ? 1.0 : 0.0;
    if (K <= 0.0) return 1.0;

    auto [a, b] = truncation_interval(T, p);
    double bma  = b - a;

    // Payoff coefficients
    auto V = payoff_coefficients(K, p.S, a, b, N);

    // Sum: Σ_{k=0}^{N-1} Re[φ(kπ/(b-a)) * exp(-ikπa/(b-a))] * V_k
    // with the k=0 term halved (standard COS convention)
    double sum = 0.0;
    for (int k = 0; k < N; ++k) {
        if (V[k] == 0.0) continue;
        double u     = k * M_PI / bma;
        cd phi       = bates::characteristic_function(cd(u, 0.0), T, p);
        cd exp_term  = std::exp(cd(0.0, -u * a));
        double re    = std::real(phi * exp_term);
        double weight = (k == 0) ? 0.5 : 1.0;
        sum += weight * re * V[k];
    }
    return std::max(0.0, std::min(1.0, sum));
}

/**
 * Price a cash-or-nothing binary call (discounted).
 *   C_bin = e^{-rT} * Q(S(T) > K)
 */
inline double binary_call(
        double K, double T, const bates::Params& p, int N = 256) {
    return std::exp(-p.r * T) * binary_call_prob(K, T, p, N);
}

/**
 * Price a batch of cash-or-nothing binary calls over a spot grid.
 *
 * This reuses the COS characteristic-function terms that are common across the
 * whole price axis and only recomputes the payoff coefficients for each spot.
 */
inline std::vector<double> binary_call_batch(
        double K, double T, const bates::Params& p,
        const std::vector<double>& spots, int N = 256) {
    std::vector<double> prices(spots.size(), 0.0);
    if (spots.empty()) {
        return prices;
    }
    if (K <= 0.0) {
        double discounted = (T <= 0.0) ? 1.0 : std::exp(-p.r * T);
        std::fill(prices.begin(), prices.end(), discounted);
        return prices;
    }
    if (T <= 0.0) {
        for (std::size_t idx = 0; idx < spots.size(); ++idx) {
            prices[idx] = (spots[idx] > K) ? 1.0 : 0.0;
        }
        return prices;
    }

    auto [a, b] = truncation_interval(T, p);
    double bma = b - a;

    std::vector<double> cos_weights(N, 0.0);
    for (int k = 0; k < N; ++k) {
        double u = k * M_PI / bma;
        cd phi = bates::characteristic_function(cd(u, 0.0), T, p);
        cd exp_term = std::exp(cd(0.0, -u * a));
        double re = std::real(phi * exp_term);
        double weight = (k == 0) ? 0.5 : 1.0;
        cos_weights[k] = weight * re;
    }

    double discount = std::exp(-p.r * T);
    for (std::size_t idx = 0; idx < spots.size(); ++idx) {
        double S = spots[idx];
        if (S <= 0.0) {
            prices[idx] = 0.0;
            continue;
        }

        auto V = payoff_coefficients(K, S, a, b, N);
        double sum = 0.0;
        for (int k = 0; k < N; ++k) {
            if (V[k] == 0.0) continue;
            sum += cos_weights[k] * V[k];
        }
        prices[idx] = discount * std::max(0.0, std::min(1.0, sum));
    }
    return prices;
}

/**
 * Price a cash-or-nothing binary put (pays $1 if S(T) < K).
 *   P_bin = e^{-rT} * Q(S(T) < K) = e^{-rT} - C_bin
 */
inline double binary_put(
        double K, double T, const bates::Params& p, int N = 256) {
    return std::exp(-p.r * T) - binary_call(K, T, p, N);
}

/**
 * Finite-difference Greeks for the binary call.
 * All use central differences except theta (forward difference).
 */
struct Greeks {
    double delta;  // ∂C/∂S
    double vega;   // ∂C/∂v0
    double theta;  // ∂C/∂T  (negative = time decay)
    double lambda_sens; // ∂C/∂λ
};

inline Greeks compute_greeks(
        double K, double T, const bates::Params& p, int N = 128) {
    const double dS  = std::max(std::abs(p.S) * 1e-4, 1e-6);
    const double dv  = std::max(std::abs(p.v0) * 1e-4, 1e-6);
    const double dT  = 1.0 / 365.25;   // 1 day
    const double dl  = std::max(std::abs(p.lambda) * 1e-4, 1e-6);

    auto bump = [&](bates::Params q) { return binary_call(K, T, q, N); };
    double base = binary_call(K, T, p, N);

    auto central_or_forward = [&](auto apply_bump, double h) {
        bates::Params pu = p;
        bates::Params pd = p;
        apply_bump(pu, h);
        apply_bump(pd, -h);
        if (pd.S > 0.0 && pd.v0 >= 0.0 && pd.lambda >= 0.0) {
            return (bump(pu) - bump(pd)) / (2.0 * h);
        }
        return (bump(pu) - base) / h;
    };

    double delta = central_or_forward(
        [](bates::Params& q, double h) { q.S += h; }, dS);

    double vega = central_or_forward(
        [](bates::Params& q, double h) { q.v0 += h; }, dv);

    double theta = 0.0;
    if (T > dT) {
        theta = (binary_call(K, T + dT, p, N) - binary_call(K, T - dT, p, N))
                / (2.0 * dT);
    } else {
        theta = (binary_call(K, T + dT, p, N) - base) / dT;
    }

    double lsens = central_or_forward(
        [](bates::Params& q, double h) { q.lambda += h; }, dl);

    return {delta, vega, theta, lsens};
}

} // namespace cos_pricer
