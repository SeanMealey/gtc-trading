#pragma once
#include <complex>
#include <cmath>

/**
 * Bates (1996) Stochastic Volatility with Jumps model.
 *
 * Under risk-neutral measure Q:
 *   dS = (r - q - λm̄) S dt + √v S dW₁ + S dJ
 *   dv = κ(θ - v) dt + σₕ √v dW₂
 *   dW₁·dW₂ = ρ dt
 *   J ~ compound Poisson, intensity λ, log-jump ~ N(μⱼ, σⱼ²)
 *   m̄ = exp(μⱼ + σⱼ²/2) - 1
 */

namespace bates {

struct Params {
    double S;       // spot price
    double v0;      // initial variance
    double kappa;   // mean-reversion speed
    double theta;   // long-run variance
    double sigma_v; // vol of vol
    double rho;     // spot-variance correlation
    double lambda;  // jump intensity (jumps/year)
    double mu_j;    // mean log-jump size
    double sigma_j; // std dev log-jump size
    double r;       // risk-free rate (annualised)
    double q;       // dividend / carry yield (annualised)
};

using cd = std::complex<double>;

/**
 * Bates characteristic function for log(S(T)/S(0)).
 *
 * φ(u) = φ_Heston(u) × exp(λT[E[e^{iuY}] - 1 - iu·m̄])
 * where Y ~ N(μⱼ, σⱼ²), so
 *   E[e^{iuY}] = exp(iuμⱼ - 0.5σⱼ²u²)
 *
 * Heston component uses the numerically stable formulation from
 * Albrecher et al. (2007) to avoid branch-cut discontinuities.
 */
inline cd characteristic_function(cd u, double T, const Params& p) {
    const double kappa = p.kappa;
    const double theta = p.theta;
    const double sigma = p.sigma_v;
    const double rho   = p.rho;
    const double v0    = p.v0;
    const double r     = p.r;
    const double q     = p.q;

    // --- Heston component (stable form) ---
    cd xi    = kappa - sigma * rho * u * cd(0, 1);
    cd d     = std::sqrt(xi * xi + sigma * sigma * u * (u + cd(0, 1)));
    cd g2    = (xi - d) / (xi + d);
    cd expdT = std::exp(-d * T);

    cd A = (xi - d) * T - 2.0 * std::log((1.0 - g2 * expdT) / (1.0 - g2));
    cd B = (xi - d) * (1.0 - expdT) / (sigma * sigma * (1.0 - g2 * expdT));

    // --- Drift term ---
    cd drift = u * cd(0, 1) * (r - q) * T;

    cd heston = std::exp(drift + (kappa * theta / (sigma * sigma)) * A + v0 * B);

    // --- Jump component ---
    double m_bar = std::exp(p.mu_j + 0.5 * p.sigma_j * p.sigma_j) - 1.0;
    cd jump_cf = std::exp(cd(0, 1) * u * p.mu_j
                          - 0.5 * p.sigma_j * p.sigma_j * u * u);
    cd jump_comp = std::exp(p.lambda * T * (jump_cf - 1.0 - cd(0, 1) * u * m_bar));

    return heston * jump_comp;
}

} // namespace bates
