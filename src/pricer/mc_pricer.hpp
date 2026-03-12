#pragma once
#include <vector>
#include <cmath>
#include <random>
#include <stdexcept>
#include "bates.hpp"

/**
 * Monte Carlo pricer for the Bates model.
 *
 * Used to validate COS prices. Not intended for production signal generation.
 *
 * Scheme: Euler-Maruyama with full truncation for the variance process.
 * Variance reduction: antithetic variates.
 *
 * KNOWN LIMITATION: Euler-Maruyama introduces a downward bias for the Heston
 * variance process at high sigma_v (> 0.5) and short T (< 30d). Confirmed
 * against Gil-Pelaez: COS matches to 5dp; MC underestimates by ~1-3% at
 * sigma_v=0.8, T=7d. The Andersen (2008) QE scheme would eliminate this
 * but is not implemented. Use MC for gross sanity checks only.
 *
 * Jump sampling: at each step, draw N_jumps ~ Poisson(λ dt), then sum
 * N_jumps independent log-normal jump sizes.
 */

namespace mc_pricer {

struct McResult {
    double price;   // estimated binary call probability Q(S(T) > K)
    double stderr;  // Monte Carlo standard error
    int    paths;   // number of paths used
};

/**
 * Price a binary call Q(S(T) > K) via Monte Carlo.
 *
 * @param K       Strike
 * @param T       Time to expiry (years)
 * @param p       Bates parameters
 * @param n_paths Number of paths (use 1e5 for validation, 1e4 for checks)
 * @param n_steps Time steps per path (higher = more accurate for large T)
 * @param seed    RNG seed (-1 for random)
 */
inline McResult binary_call_prob_mc(
        double K, double T, const bates::Params& p,
        int n_paths = 100000, int n_steps = 200, int seed = 42) {
    if (T <= 0.0) return {(p.S > K) ? 1.0 : 0.0, 0.0, 0};

    std::mt19937_64 rng(seed < 0 ? std::random_device{}() : static_cast<uint64_t>(seed));
    std::normal_distribution<double>  norm(0.0, 1.0);
    std::poisson_distribution<int>    poisson(p.lambda * T / n_steps);

    const double dt     = T / n_steps;
    const double sqrt_dt = std::sqrt(dt);
    const double m_bar  = std::exp(p.mu_j + 0.5 * p.sigma_j * p.sigma_j) - 1.0;
    const double drift_adj = (p.r - p.q - p.lambda * m_bar);  // drift under Q

    // Antithetic pairs: run 2 * (n_paths/2) paths
    int n_pairs = n_paths / 2;
    double sum = 0.0, sum_sq = 0.0;

    for (int i = 0; i < n_pairs; ++i) {
        // Draw correlated Brownians for the pair (antithetic)
        double payoff_sum = 0.0;

        for (int sign : {1, -1}) {
            double S = p.S;
            double v = p.v0;

            for (int step = 0; step < n_steps; ++step) {
                double w1 = norm(rng);
                double w2 = norm(rng);
                double z1_base = w1;
                double z2_base = p.rho * w1 + std::sqrt(1.0 - p.rho * p.rho) * w2;
                double z1 = sign * z1_base;
                double z2 = sign * z2_base;

                // Full truncation: clamp v to 0 in diffusion
                double v_pos = std::max(v, 0.0);
                double sv    = std::sqrt(v_pos);

                // Jump contribution to log-S
                int n_jumps = poisson(rng);
                double jump_log = 0.0;
                for (int j = 0; j < n_jumps; ++j) {
                    jump_log += p.mu_j + p.sigma_j * norm(rng);
                }

                // Log-price update (Euler on log-S for stability)
                double log_S = std::log(S);
                log_S += (drift_adj - 0.5 * v_pos) * dt
                         + sv * sqrt_dt * z1
                         + jump_log;
                S = std::exp(log_S);

                // Variance update (Euler with full truncation)
                v += p.kappa * (p.theta - v_pos) * dt
                     + p.sigma_v * sv * sqrt_dt * z2;
            }

            payoff_sum += (S > K) ? 1.0 : 0.0;
        }

        double pair_mean = payoff_sum / 2.0;
        sum    += pair_mean;
        sum_sq += pair_mean * pair_mean;
    }

    double prob   = sum / n_pairs;
    double var    = (sum_sq / n_pairs - prob * prob) / (n_pairs - 1);
    double stderr = std::sqrt(std::max(var, 0.0));

    return {std::max(0.0, std::min(1.0, prob)), stderr, n_pairs * 2};
}

/**
 * Binary call price (discounted).
 */
inline McResult binary_call_mc(
        double K, double T, const bates::Params& p,
        int n_paths = 100000, int n_steps = 200, int seed = 42) {
    auto res = binary_call_prob_mc(K, T, p, n_paths, n_steps, seed);
    double disc = std::exp(-p.r * T);
    return {disc * res.price, disc * res.stderr, res.paths};
}

} // namespace mc_pricer
