# Live Market Maker Outline

## Current State

As of April 11, 2026, the repo is strongest in research and pricing infrastructure, not execution.

Implemented and reusable:

- Bates pricer C++ extension and Python bindings in `src/pricer/`
- parameter loading and calibration utilities in `src/calibration/`
- Gemini public market-data collectors in `src/data_collection/`
- signal generation in `src/strategy/signal.py`
- sizing logic in `src/strategy/sizing.py`
- scenario-matrix portfolio risk controls in `src/strategy/scenario_matrix.py` and `src/strategy/scenario_risk.py`
- live public-quote dashboard in `src/live_dashboard.py`

- Gemini auth/connectivity probe in `src/test_gemini_api.py`
- scenario-risk unit coverage in `tests/test_scenario_matrix.py`

Not implemented yet:

- private Gemini execution client module
- live runner that submits and reconciles orders
- real fill handling, partial-fill handling, and cancel flow
- reconciliation against Gemini positions and order history on startup
- live trade ledger separate from simulated paper blotters
- production config, service unit, and deploy runbook for live trading
- kill switch, stale-market guardrails, and operational alerting

## What This Means

This project is not blocked on pricing research. It is blocked on exchange integration and production operations.

The fastest path to deployment is to turn the existing public-data scan loop into a real live runner, not to spend more time on simulation.

## Finish Plan

### Phase 1: Build Gemini Execution Layer

Create `src/strategy/execution.py` with:

- HMAC-SHA384 signing using `GEMINI_API_KEY` and `GEMINI_API_SECRET`
- `get_positions()`
- `get_active_orders()`
- `get_order_history()`
- `place_order()`
- `cancel_order()`
- consistent request/response normalization
- explicit handling for 4xx, 5xx, timeouts, and malformed responses

Definition of done:

- the module can authenticate
- it can place a tiny IOC order
- it can fetch the resulting order status and filled quantity

### Phase 2: Replace the Paper Loop with a Live Runner

Create `src/strategy/runner.py` that:

- fetches active BTC events and top-of-book quotes
- prices contracts with current Bates parameters
- generates signals with the existing signal module
- sizes trades with the existing sizing module
- runs scenario-risk checks before submission
- submits IOC orders through `execution.py`
- records actual order IDs, filled quantity, avg execution price, fees, and timestamps
- settles or closes internal state from Gemini truth, not from assumed local fills

Key rule:

- never assume a fill because the quote looked tradable

### Phase 3: Add State Reconciliation and Recovery

On startup, the runner should:

- load local position state
- fetch Gemini open positions
- fetch recent order history
- reconcile mismatches before trading
- refuse to trade if local and exchange state disagree materially

This is the minimum safe restart behavior for production.

### Phase 4: Harden Risk Controls for Live Trading

Add live-only safeguards around the existing model/risk logic:

- max notional per order
- max net exposure by expiry bucket
- max exposure by settlement index
- stale parameter age limit
- stale quote / crossed-book / one-sided-book rejection
- max consecutive API failures before circuit break
- kill switch via env var or file flag
- daily loss limit and daily filled-notional cap

### Phase 5: Trade Accounting and Observability

Create a real trade ledger under `data/strategy/trades.csv` or a SQLite store with:

- order ID
- client order ID
- event ticker
- instrument
- side
- outcome
- submitted price
- filled quantity
- avg fill price
- fee
- model price at decision time
- edge at decision time
- scenario-gate decision
- params version / params timestamp

Also add:

- structured logs
- heartbeat logging
- PnL and exposure summary command
- alert hooks for runner down, API auth failure, and circuit-break events

### Phase 6: Production Deployment

Add live deployment artifacts:

- a locked dependency file such as `requirements-live.txt` or `pyproject.toml`
- a live config file without paper-only fields
- a `deploy/live-runner.service`
- a `docs/live-runner-ec2.md`
- environment variable setup instructions for Gemini credentials

Recommended deploy order:

1. Build the Linux pricer extension on the target host.
2. Run `src/test_gemini_api.py` with live credentials.
3. Run the live runner in `--once` or dry startup mode without order submission.
4. Enable order submission for one tiny order size.
5. Monitor logs, fills, reconciliation, and fees.
6. Only then widen limits.

## Priority Order

If the goal is to get live fastest, do the work in this order:

1. `execution.py`
2. `runner.py`
3. reconciliation on startup
4. live ledger
5. kill switch and failure circuit breaker
6. live deploy docs and service unit
7. monitoring and alerting

## Residual Risks

Even after implementation, these areas need explicit validation before real size:

- exact Gemini prediction-market order payload shape
- how Gemini reports partial fills and fees for IOC orders
- whether position endpoints reflect fills immediately enough for reconciliation
- settlement/outcome polling behavior after expiry
- exchange-specific edge cases around cancelled remainder quantity
