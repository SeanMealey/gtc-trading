# Inventory Skewing Implementation Plan

This document outlines how to add inventory-skewed trade logic on top of the current scenario-matrix risk controls.

The goal is not to replace the scenario matrix. The goal is to add a softer, market-maker-style preference layer that pushes the strategy toward trades that improve the book and away from trades that worsen it.


## Summary

Recommended architecture:

- Raw signal generation remains the first step.
- Inventory skew adjusts the effective entry threshold, candidate score, and optionally desired size.
- The scenario matrix remains the final hard risk gate.

This keeps the system intuitive:

- inventory skew = preference
- scenario matrix = constraint


## Why This Is Worth Doing

The current strategy can already reject bad trades using the scenario matrix, but it still treats all positive-edge trades too similarly before the hard gate.

Inventory skewing adds value because it can:

- prioritize trades that flatten the book
- deprioritize trades that increase directional imbalance
- reduce unnecessary scenario-matrix rejections by discouraging obviously bad candidates earlier
- make trade selection more consistent with the portfolio objective instead of raw edge alone


## Design Principles

### 1. Keep Hard and Soft Logic Separate

The inventory-skew layer should not duplicate the scenario gate.

- Inventory skew should modify trade attractiveness.
- Scenario matrix should still make the final accept/reject decision.

This avoids redundant logic and makes tuning easier.

### 2. Use a Small Number of Inputs

For the first version, only use:

- expected P&L improvement
- terminal flatness impact
- max-loss impact

These already align with the agreed scenario policy.

### 3. Make the First Version Transparent

Every trade decision should be explainable from logged values such as:

- raw edge
- skew penalty or skew credit
- effective min edge
- scenario metrics before and after the candidate


## Proposed Decision Flow

The intended flow after implementation:

1. Generate the raw signal exactly as today.
2. Compute the candidate trade's marginal impact on the current portfolio.
3. Convert that impact into an inventory skew adjustment.
4. Adjust the effective minimum edge and optionally the candidate score.
5. If the trade still qualifies, run it through the scenario-matrix hard gate.
6. If accepted, execute at the approved quantity.

This means the skewing logic happens before the final scenario approval, but uses the same portfolio context.


## Phase 1: Add Inventory-Aware Scoring

### Objective

Add a soft trade preference layer without changing the final scenario gate.

### Proposed Core Quantity

For each candidate, compute:

- `delta_expected_pnl = candidate_expected_pnl - current_expected_pnl`
- `delta_flatness = candidate_terminal_flatness - current_terminal_flatness`
- `delta_max_loss = candidate_max_loss - current_max_loss`

Interpretation:

- `delta_expected_pnl > 0` is good
- `delta_flatness < 0` is good
- `delta_max_loss > 0` is good

### Proposed Derived Score

Introduce an inventory adjustment term:

`inventory_adjustment = ev_credit + flatness_credit + max_loss_credit`

Where:

- `ev_credit` rewards positive expected-P&L improvement
- `flatness_credit` rewards flatter terminal surface and penalizes worse flatness
- `max_loss_credit` rewards improved worst-case loss and penalizes deterioration

Then define:

- `effective_edge = raw_edge + inventory_adjustment`

or equivalently:

- `effective_min_edge = base_min_edge - inventory_adjustment`

The second form is usually easier to reason about operationally.


## Phase 2: Skew the Entry Threshold

### Objective

Make the strategy more willing to take helpful trades and less willing to take harmful ones.

### Recommended First Implementation

Keep the current signal logic mostly intact and add a new post-signal filter:

- if a candidate improves the book, allow a smaller required edge
- if a candidate worsens the book, require a larger edge

Proposed formula:

- `required_edge = base_min_edge + flatness_penalty + max_loss_penalty - ev_credit`

Then require:

- `raw_edge >= required_edge`

### Why This Is the Best First Version

- simple to implement
- easy to log and tune
- compatible with current `buy_min_edge` and `sell_min_edge`
- does not require rewriting the signal module


## Phase 3: Candidate Ranking

### Objective

When several trades are available at once, prefer the ones that help the portfolio most.

### Proposed Change

Add a ranking score such as:

`candidate_score = raw_edge + w_ev * delta_expected_pnl - w_flatness * delta_flatness + w_loss * delta_max_loss`

Where:

- better expected P&L raises score
- worse flatness lowers score
- better max loss raises score

This score should be used to rank candidates before execution, especially in the backtest and paper runner loops.

### Important Note

This ranking should be used to choose between candidates, not to replace the hard scenario gate.


## Phase 4: Optional Size Skew

### Objective

Use inventory impact not just for yes/no decisions, but also for preferred size.

### Possible Extension

Before the scenario matrix resize loop, adjust the starting requested quantity:

- helpful trade: keep requested size or slightly increase it
- harmful trade: reduce requested size before scenario evaluation

This should be introduced only after the threshold-skew logic is stable.

The current scenario resize logic already acts as a safety backstop, so this phase is optional rather than required.


## Required Code Changes

### 1. Add a Dedicated Inventory Skew Module

Create a new module, likely:

- `src/strategy/inventory_skew.py`

Responsibilities:

- translate scenario comparison metrics into skew adjustments
- compute effective minimum edge
- compute optional ranking score
- return structured diagnostics

Suggested core object:

- `InventorySkewDecision`

Possible fields:

- `raw_edge`
- `effective_required_edge`
- `passes_inventory_filter`
- `inventory_adjustment`
- `delta_expected_pnl`
- `delta_flatness`
- `delta_max_loss`
- `score`
- `reasons`


### 2. Reuse Existing Scenario Comparison Output

Do not reprice anything new for the first version.

The current scenario pipeline already produces the needed metrics:

- current metrics
- candidate metrics
- improvement booleans

The inventory skew module should consume a `ScenarioComparison` rather than rebuilding surfaces.


### 3. Integrate Into Backtest

Integration point:

- after raw candidate generation and basic sizing
- before final scenario-gate accept/reject

Flow in backtest:

1. compute scenario comparison for requested quantity
2. compute inventory skew decision
3. reject or deprioritize candidates that fail the inventory filter
4. run surviving candidates through the final scenario gate

For the backtest, it may be useful to rank multiple same-timestamp candidates by inventory-aware score.


### 4. Integrate Into Paper Runner

Integration point:

- after raw signal generation
- before paper fill logging

Flow in paper runner:

1. generate signal
2. run inventory skew evaluation
3. print why the candidate was helped or penalized
4. then run the scenario hard gate


### 5. Add Config Knobs

Add config settings to `StrategyConfig` and expose them in `config/paper.ec2.json`.

Recommended first-pass config fields:

- `enable_inventory_skew: bool`
- `inventory_skew_ev_weight: float`
- `inventory_skew_flatness_weight: float`
- `inventory_skew_max_loss_weight: float`
- `inventory_skew_max_edge_credit: float`
- `inventory_skew_max_edge_penalty: float`
- `inventory_skew_require_positive_score: bool`

Optional future fields:

- `inventory_skew_size_multiplier_min`
- `inventory_skew_size_multiplier_max`


## Logging and Observability

Add trade-entry metadata for:

- raw edge
- effective required edge
- inventory adjustment
- `delta_expected_pnl`
- `delta_flatness`
- `delta_max_loss`
- inventory score
- whether the trade passed due to skew credit or failed due to skew penalty

This should be added to:

- backtest trade CSV
- paper trade logs
- optional dashboard columns later


## Rollout Strategy

### Step 1

Implement the module and logging only.

- compute the skew adjustment
- store it
- do not use it to block trades yet

Purpose:

- validate whether the adjustment matches intuition

### Step 2

Turn on threshold skewing only.

- use skew to modify required edge
- keep scenario matrix as-is

Purpose:

- measure impact on trade selection without changing hard risk behavior

### Step 3

Add candidate ranking by inventory-aware score.

Purpose:

- improve which trades are chosen when capital or position slots are constrained

### Step 4

Only if needed, add size skewing.


## Testing Plan

Add unit tests for:

- trade that improves expected P&L gets a positive skew credit
- trade that worsens flatness gets a penalty
- trade that worsens max loss gets a penalty
- effective required edge is lower for helpful trades
- effective required edge is higher for harmful trades
- scenario gate still rejects trades that violate hard limits even if skew score is positive

Add integration tests for:

- backtest candidate ranking changes when two trades have similar raw edge but different portfolio impact
- paper runner logs the skew adjustment


## Risks and How to Avoid Them

### Risk 1: Double Counting Scenario Metrics

If the skew logic and scenario gate both penalize the exact same effect too strongly, the strategy may become overly conservative.

Mitigation:

- keep inventory skew soft
- keep scenario gate hard
- use modest skew weights

### Risk 2: Overfitting the Weights

Too many knobs can make the system fragile.

Mitigation:

- start with only 3 weights
- keep default values small
- tune from broad behavior, not trade-by-trade anecdotes

### Risk 3: Computational Overhead

Inventory skew depends on scenario comparison, which can be expensive.

Mitigation:

- reuse the existing scenario comparison already computed for the hard gate
- do not introduce a second pricing pass


## Recommended First Implementation

The best first version is:

1. Add `inventory_skew.py`.
2. Reuse `ScenarioComparison` to compute:
   - `delta_expected_pnl`
   - `delta_flatness`
   - `delta_max_loss`
3. Convert those into an `effective_required_edge`.
4. Log the adjustment in backtest and paper trading.
5. Use the skewed edge threshold before the hard scenario gate.

This keeps the implementation small, coherent, and aligned with the current architecture.


## Suggested Future Order

1. Inventory skew diagnostics only.
2. Threshold skewing.
3. Candidate ranking by inventory-aware score.
4. Optional size skewing.
5. Dashboard visualization of inventory skew attribution.
