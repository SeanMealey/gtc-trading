# GTC Trading Project Description

## What This Repository Is

This repository is a research and live-trading system for Gemini BTC prediction markets. These markets are binary contracts, typically named like `GEMI-BTC2603062200-HI69000`, that pay $1 if BTC settles above the strike at expiry and $0 otherwise.

The project combines market-data collection, derivatives calibration, fast model pricing, portfolio risk controls, and a live runner that can quote or trade Gemini prediction-market contracts. The main strategy uses a Bates stochastic-volatility-with-jumps model to estimate fair values for binary BTC contracts, then compares those fair values with Gemini bid/ask markets to decide whether to trade or quote.

The repo is not just a notebook or backtest. It contains a deployable live runner, live configuration, systemd deployment notes, execution clients, persistent position and trade logs, unit tests, and operational safeguards such as reconciliation, circuit breakers, and a kill switch.

## What It Does

At a high level, the system:

1. Collects BTC spot, options, and prediction-market data.
2. Calibrates a Bates model to Deribit BTC options implied volatility.
3. Uses a C++ COS pricer to compute fair prices for Gemini binary BTC contracts.
4. Scans active Gemini prediction markets.
5. Filters contracts by liquidity, spread, expiry, model confidence, and risk limits.
6. Generates trading signals or resting market-maker quotes.
7. Sizes orders using flat or Kelly-style sizing.
8. Applies scenario-matrix risk controls and inventory-aware skew.
9. Sends orders to Gemini when live trading is enabled.
10. Persists positions, order attempts, fills, and decision context for restart and auditability.

The system supports two related trading styles:

- **Liquidity-taking mode**: if the model price is above the ask by enough edge, buy; if the model price is below the bid by enough edge, sell.
- **Market-making mode**: post resting bid and ask quotes around model value, adjusted for required edge, maker fees, time to expiry, inventory, and current book constraints.

## Repository Layout

### `src/pricer/`

This directory contains the Bates pricing engine:

- `bates.hpp` implements the Bates stochastic-volatility-with-jumps model.
- `cos_pricer.hpp` implements COS-method pricing.
- `mc_pricer.hpp` contains Monte Carlo pricing support.
- `bindings.cpp` exposes the C++ pricer to Python through pybind11.
- `build.sh` builds the local Python extension.
- `bates_pricer.cpython-312-darwin.so` is a built macOS extension currently checked into the repo.

Live pricing depends on this extension being built for the host platform. The deployment docs note that the extension must be rebuilt on Linux before running live.

### `src/calibration/`

Calibration code turns Deribit BTC options data into Bates parameters:

- `implied.py` fits Bates parameters to Deribit implied volatility data.
- `implied_historical.py` and `build_params_history.py` support historical parameter analysis.
- `params.py` defines the `BatesParams` structure and serialization helpers.

The current live config expects calibrated parameters at:

```text
data/deribit/bates_params_implied.json
```

The live runner can periodically refresh the Deribit option chain and recalibrate while running, controlled by `calibration_interval_hours` in `config/live.json`.

### `src/data_collection/`

This directory contains scripts for acquiring source data:

- Gemini BTC prediction-market events, candles, trades, and settlements.
- Gemini BTC spot data.
- Binance supplemental BTC spot data.
- Deribit options chains for calibration.

Collected data is stored under `data/`, especially:

- `data/gemini_prediction_markets/`
- `data/gemini_spot/`
- `data/deribit/`

### `src/strategy/`

This is the core trading system.

Important modules include:

- `config.py`: central `StrategyConfig` dataclass for signal thresholds, sizing, risk, execution, logging, calibration, and market-making settings.
- `signal.py`: pure signal-generation logic for deciding when model value crosses the market by enough edge.
- `sizing.py`: flat and Kelly-style order sizing.
- `quoting.py`: pure market-maker quote-generation logic.
- `quote_manager.py`: tracks resting market-maker quotes and decides when to replace or cancel them.
- `inventory_skew.py`: adjusts trading or quoting behavior based on current inventory and scenario quality.
- `scenario_matrix.py`: builds scenario P&L surfaces across BTC prices and future evaluation times.
- `scenario_risk.py`: applies portfolio-level risk gates to candidate trades.
- `execution.py`: Gemini execution and market-data client, including REST signing and Fast API websocket support.
- `position_log.py`: persistent local position state.
- `trade_ledger.py`: append-only ledger for order decisions, attempts, fills, and dry-run records.
- `runner.py`: the live strategy loop that connects all of the above.

### `src/live_dashboard.py`

This is a terminal dashboard for monitoring active Gemini BTC prediction markets. It fetches active events, reads Bates parameters, prices contracts with the C++ pricer, and displays model prices versus market bid/ask using Rich.

The dashboard is separate from the live strategy. It shares the pricer and calibration parameters, but it does not depend on the trading runner.

### `config/live.json`

This is the live runtime configuration. It controls:

- edge thresholds and model filters
- liquidity gates
- sizing mode and capital assumptions
- scenario risk and inventory skew
- calibration cadence and parameter paths
- position, trade, quote, and runner log paths
- Gemini REST and Fast API connection settings
- reference spot-price source
- order-submission flags
- live safety caps and circuit breakers
- market-making behavior

The checked-in `config/live.json` currently has `submit_orders: true`, `dry_run: false`, and `mm_enabled: true`, so it is configured for live market-making rather than paper-only use.

### `docs/` and Planning Documents

The repo includes several design and operational documents:

- `docs/live-market-maker-outline.md`
- `docs/live-runner-deploy.md`
- `strategy_implementation_description.md`
- `inventory_skewing_plan.md`
- `scenario_matrix_live_integration_plan.md`
- `scenario_matrix_further_optimizations.md`
- `directional_exposure_solution.md`

Some older docs describe work that was not implemented yet at the time they were written. The code now includes a live execution client, runner, ledger, deployment files, market-making logic, and broader risk controls.

### `tests/`

The test suite covers the strategy modules and live-runner behavior without requiring real Gemini network access. Tests use mocks for Gemini execution and, where needed, the C++ pricer.

Covered areas include:

- signal and quoting behavior
- sizing and inventory skew
- scenario matrix and risk checks
- position logging and trade ledger behavior
- execution client normalization
- live runner decision flow
- quote-manager behavior

## How The System Works

### 1. Data And Calibration

The project starts with market data. Deribit BTC options data is collected and used to fit a Bates model. The calibration process fits model-implied volatility to market implied volatility, then writes the resulting parameters as JSON.

Those parameters represent the current model view of BTC dynamics: spot, rates, variance, mean reversion, volatility of variance, correlation, and jump parameters.

### 2. Contract Discovery

The live runner asks Gemini for active BTC prediction-market events. Each event contains contracts with instrument symbols, bid/ask prices, sizes, expiry metadata, and `totalShares` liquidity information.

Gemini BTC binary instruments encode their expiry and strike in the symbol. For example, `GEMI-BTC2603062200-HI69000` means a BTC high-style binary contract with a timestamp-like expiry code and a 69000 strike.

### 3. Model Pricing

For each eligible contract, the runner parses the strike and expiry, computes time to expiry, and calls the C++ Bates/COS pricer to estimate the fair binary price.

The model output is a probability-like price between 0 and 1, matching the binary payout format.

### 4. Signal Or Quote Generation

In liquidity-taking mode, `signal.py` compares model price with the live bid/ask:

- Buy when `model_price - ask` is large enough.
- Sell when `bid - model_price` is large enough.
- Skip when the edge is too small, the market is one-sided, the spread is too wide, the contract is illiquid, or expiry/model filters fail.

In market-making mode, `quoting.py` computes resting quotes around model value:

- required edge increases with model distance from 0.5
- required edge includes expected maker fees
- quote placement avoids crossing the book
- quote sizes are bounded by config and inventory limits
- quoting is disabled near expiry or outside configured model/time bands
- existing inventory can force reduce-only quoting

`quote_manager.py` then handles quote placement, replacement, stale quote cancellation, and reconciliation with exchange-side active orders.

### 5. Sizing And Portfolio Risk

Candidate orders are sized by either:

- flat dollar allocation per trade
- fractional Kelly sizing for binary options

Before submission, the runner applies live caps such as max quantity, max notional per order, total notional, max open positions, daily filled-notional cap, and daily loss limit.

When enabled, scenario risk builds a grid over BTC prices and future times. It values current positions plus the candidate trade across that grid, producing a P&L surface. Candidate trades can then be rejected or resized if they worsen configured metrics such as max loss, downside, flatness, variance, delta, pin risk, or expected P&L.

Inventory skew adds another layer: it can reward trades that improve portfolio shape and penalize trades that worsen concentrated exposure.

### 6. Execution

`execution.py` provides the Gemini execution layer. It supports:

- HMAC-SHA384 REST authentication for private endpoints
- account/position/order snapshots through Gemini REST endpoints
- public event and book access
- Fast API websocket market-data subscriptions
- Fast API websocket order placement and cancellation when configured
- normalized `OrderResult` objects
- dry-run behavior for testing decisions without submitting real orders

The live runner never treats a visible quote as a guaranteed fill. It records Gemini order responses and uses actual reported fill quantity, average execution price, fees, and status.

### 7. State, Logging, And Recovery

The live runner persists local state to configured files:

- positions: `data/strategy/positions.live.json`
- trade ledger: `data/strategy/trades.live.csv`
- runner log: `logs/live_runner.log`
- dry quotes: `data/strategy/dry_quotes.live.csv`

On startup, the runner can reconcile local positions against Gemini positions, active orders, and recent order history. If the state differs beyond configured tolerance, startup aborts rather than trading from a bad book.

Operational controls include:

- a file-based kill switch at `logs/KILL_SWITCH`
- max consecutive API failure circuit breaker
- daily loss limit
- daily filled-notional cap
- stale parameter checks
- stale/missing market quote cleanup
- heartbeat logging

## How To Run It

### Install Dependencies

Live Python dependencies are listed in:

```text
deploy/requirements-live.txt
```

They include numpy, pandas, scipy, pybind11, rich, and websockets.

### Build The Pricer

Build the C++ pricer extension for the current machine:

```bash
cd src/pricer
./build.sh
```

The checked-in `.so` is platform-specific, so a Linux deployment must rebuild it locally.

### Run Tests

From the repo root:

```bash
python -m pytest
```

The tests are designed to avoid real exchange calls by mocking Gemini clients and pricing where appropriate.

### Run The Dashboard

From the repo root:

```bash
python src/live_dashboard.py
```

Optional arguments let you choose the parameter file or refresh interval.

### Run The Live Runner

The deployment runbook is in `docs/live-runner-deploy.md`. A typical dry smoke test is:

```bash
PYTHONPATH=src python -m strategy.runner --config config/live.json --once
```

Before running against a real account, verify Gemini credentials, Prediction Markets permissions, the Prediction Markets Terms of Service, the live config, and the current values of `submit_orders`, `dry_run`, and `mm_enabled`.

## Key Safety Notes

This repository can be configured to submit real Gemini orders. Treat `config/live.json` as a production control surface.

Important live-trading gates include:

- `submit_orders`
- `dry_run`
- `max_notional_per_order_usd`
- `max_total_notional_usd`
- `daily_filled_notional_cap_usd`
- `daily_loss_limit_usd`
- `kill_switch_path`
- `require_state_reconciliation`
- `mm_enabled`

Do not assume a local development run is paper trading. Check the config first.

## Current Project State

The repo currently contains:

- a Bates calibration and pricing stack
- Gemini prediction-market data collectors
- historical market data under `data/`
- a Rich dashboard
- a live runner
- Gemini REST and Fast API execution support
- live market-making quote logic
- scenario and inventory risk controls
- persistent position and ledger files
- deployment docs and a systemd service unit
- unit tests for the strategy layer

The main remaining operational risks are exchange-specific live behavior: partial fills, fee fields, account-event timing, settlement automation, and production monitoring/alerting. The code records detailed decision and order context, but live behavior should still be validated with very small limits before increasing size.
