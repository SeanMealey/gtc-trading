# Scenario Matrix Further Optimizations

This document captures prospective improvements to the scenario matrix system that could be implemented later. These are not required for the current version, but they represent the most likely next steps if performance, accuracy, or usability become limiting.

## 1. Volatility Surface Dynamics

### 1.1 Spot-Dependent Parameters

The current scenario matrix uses a single static `BatesParams` object for the full price-time grid. That is simple and stable, but binary options can be very sensitive to skew and smile behavior near strike.

Possible future improvement:
- Allow model parameters to vary as a function of simulated spot.
- Support conventions such as sticky-strike, sticky-delta, or a hybrid rule.
- Compare results under different smile assumptions before adopting any one convention as the default.

Expected benefit:
- More realistic repricing when BTC moves materially away from current spot.
- Better accuracy for terminal hole detection and candidate-trade evaluation.

Main risk:
- Adds model complexity and can create false precision if smile dynamics are not calibrated to observed market behavior.

### 1.2 Term Structure-Aware Parameters

The current setup also treats all time nodes with the same parameter set.

Possible future improvement:
- Use a time-aware parameter surface or parameter interpolation by expiry horizon.
- Allow near-dated binaries to use a more local calibration than longer-dated contracts.

Expected benefit:
- Better surface accuracy when contracts span meaningfully different expiries.


## 2. Surface Construction Performance

### 2.1 Reuse More COS Internals Across Time Slices

The current batch implementation vectorizes across the price axis in C++, which already removes most Python overhead. A future step would be to reuse additional COS setup terms across repeated calls.

Possible future improvement:
- Cache truncation intervals and characteristic-function terms when multiple contracts share similar horizons.
- Reuse per-time-slice terms across multiple strikes where mathematically valid.

Expected benefit:
- Additional speedup for larger books and denser grids.

Main risk:
- More complicated caching rules and a higher chance of subtle mistakes.

### 2.2 Parallelize Across Contracts or Time Rows

Once the batch pricing path is in place, the next clear performance lever is parallel execution.

Possible future improvement:
- Parallelize contract contributions in C++.
- Parallelize time-slice pricing when contracts are independent.

Expected benefit:
- Better performance for larger portfolios and finer grids.

Main risk:
- Increased implementation complexity, especially around Python bindings and deterministic behavior.

### 2.3 Sparse or Adaptive Grids

The current grid is uniform in price and time.

Possible future improvement:
- Use denser price spacing near current spot and active strikes.
- Use denser time spacing near expiry and coarser spacing further out.
- Optionally refine the grid adaptively around steep gradients or detected holes.

Expected benefit:
- Better resolution where the portfolio is most sensitive without paying full cost everywhere.

Main risk:
- Metrics like delta, theta, and hole detection become slightly more complex on non-uniform or adaptive grids.


## 3. Probability Modeling Improvements

### 3.1 Replace the Simple Lognormal Overlay

The current probability-weighted metrics use a lognormal approximation over the terminal price axis.

Possible future improvement:
- Derive probabilities directly from the Bates model.
- Use Monte Carlo or a numerical density extraction method for terminal BTC price probabilities.

Expected benefit:
- Better expected P&L and payoff-variance estimates.
- More internally consistent risk metrics.

Main risk:
- More computation and more complexity in the probability layer.


## 4. Risk Metrics and Decision Logic

### 4.1 Better Flatness Metrics

`Terminal flatness` is currently measured as the standard deviation of the terminal payoff row.

Possible future improvement:
- Add weighted flatness around current spot.
- Add local flatness near live strikes only.
- Add slope-weighted or curvature-weighted penalties.

Expected benefit:
- Better alignment between the metric and actual trading intuition.

### 4.2 Better Hole Detection

The current hole detector looks for negative contiguous terminal price ranges.

Possible future improvement:
- Rank holes by expected probability mass, not just worst P&L.
- Add hole depth, width, and recovery distance.
- Track holes over time to show whether a candidate trade genuinely improves the book.

Expected benefit:
- Better trade-selection rules for self-hedging.

### 4.3 Candidate Trade Attribution

The current comparison logic evaluates current vs candidate surfaces, but attribution is still fairly high level.

Possible future improvement:
- Break down a candidate trade’s effect on:
  - expected P&L
  - terminal flatness
  - worst-case region
  - hole reduction
  - local delta around current spot

Expected benefit:
- More interpretable entry decisions.


## 5. Dashboard and Workflow Improvements

### 5.1 Historical Snapshots of the Matrix

The current dashboard focuses on the live surface.

Possible future improvement:
- Save periodic scenario matrix snapshots.
- Compare the current matrix to prior snapshots.
- Visualize how holes and flatness evolve as trades are added.

Expected benefit:
- Easier debugging of portfolio evolution and trade selection quality.

### 5.2 Candidate Trade Preview in the UI

Possible future improvement:
- Let the dashboard load a hypothetical trade and display:
  - current surface
  - candidate surface
  - diff heatmap
  - decision reasons

Expected benefit:
- Faster discretionary review and easier validation of automated trade gates.

### 5.3 Fixed Color Scales and Regime Views

Possible future improvement:
- Keep heatmap color scales stable between updates.
- Add presets for:
  - current spot-centered view
  - strike-cluster view
  - stress view

Expected benefit:
- Easier visual interpretation over time.


## 6. Data and Logging Improvements

### 6.1 Explicit Structured Snapshot Logging

Plain-text runner logs are not ideal as a system of record for reconstructing a live book.

Possible future improvement:
- Emit structured portfolio snapshots periodically.
- Log fills, settlements, and current open inventory in machine-readable form.

Expected benefit:
- Easier recovery, diagnostics, and dashboard integration.

### 6.2 Richer Trade Metadata

Possible future improvement:
- Store scenario-matrix metrics at trade entry.
- Record whether a trade improved flatness, reduced holes, or worsened worst-case loss.

Expected benefit:
- Better post-trade analysis and easier tuning of risk rules.


## Suggested Future Priority

If future work resumes, a reasonable order would be:

1. Better structured logging and snapshotting.
2. More informative candidate-trade attribution in the dashboard.
3. Improved probability modeling.
4. Spot-dependent smile dynamics.
5. Additional C++ caching or parallelism if larger books make it necessary.
