# Strategy Implementation Description

## Overview

A liquidity-taking strategy on Gemini BTC binary prediction markets. The strategy uses a Bates stochastic-volatility-with-jumps (SVJ) model to compute fair values for binary options (contracts paying $1 if BTC > strike at expiry, else $0). When the model price falls outside the market's bid/ask spread, a limit IOC order is placed to capture the edge.

Capital allocation: small fixed amount for live evaluation. Hold all positions to settlement (no early exit in Phase 1).

---

## Critical API Findings

### Auth
Gemini REST API uses **HMAC-SHA384**, not RSA. The PEM key in `keys/` is for the **FIX protocol only** and cannot be used for REST requests. A separate REST API key with `NewOrder` and `CancelOrder` permissions must be created in the Gemini UI (Settings → API Keys).

Additionally, the Prediction Markets **Terms of Service must be accepted in the Gemini UI** before any private PM endpoint will work — otherwise all trading endpoints return 403.

### Order Types
Only **limit orders** are supported. No market orders. To take liquidity, use:
- `timeInForce: "immediate-or-cancel"` — fills what it can at the limit price, cancels the rest immediately
- Price set to the current ask (for buys) or current bid (for sells)

This is effectively a taker order.

### Outcome Field
Orders require an `outcome` field ("yes" or "no"):
- **BUY signal** (model > ask): `side="buy", outcome="yes", price=ask`
- **SELL signal** (model < bid): `side="sell", outcome="yes", price=bid`

### Volume Data
Two sources for volume gating per contract:
1. `totalShares` field on each contract in the public events endpoint (no auth needed, available now)
2. `POST /v1/prediction-markets/metrics/volume` with `eventTicker` (auth required, more detailed)

Phase 1 will use `totalShares` from the public endpoint. If auth is set up, upgrade to the volume metrics endpoint.

---

## Architecture

```
src/
  pricer/                    ← C++ Bates COS pricer (unchanged)
  calibration/               ← BatesParams, implied.py (unchanged)
  strategy/
    config.py                ← StrategyConfig dataclass (all tunable parameters)
    signal.py                ← Pure signal logic: (model, bid, ask) → Signal
    sizing.py                ← flat_size() and kelly_size() functions
    execution.py             ← Gemini REST client: auth, place_order, cancel_order
    position_log.py          ← JSON persistence for open positions
    runner.py                ← Main strategy loop (orchestrates everything)
  live_dashboard.py          ← Unchanged, separate from strategy
  test_gemini_api.py         ← API connectivity/auth test (no order placement)

data/
  strategy/
    positions.json           ← Live open position state (persisted across restarts)
    trades.csv               ← Append-only trade log (entry and settlement)
  deribit/
    bates_params_implied.json ← Calibrated params (written by implied.py)
```

The strategy modules have **no dependency on the dashboard** and the dashboard has no dependency on the strategy. Both share the pricer and calibration modules only.

---

## Module Details

### `strategy/config.py`

```python
@dataclass
class StrategyConfig:
    # ── Signal ────────────────────────────────────────────────
    min_edge: float = 0.03          # minimum model - ask (buy) or bid - model (sell)
    model_min: float = 0.15         # reject contracts where model < model_min
    model_max: float = 0.85         # reject contracts where model > model_max
    max_t_days: float = 7.0         # reject contracts with T > max_t_days
    # No min_t — targeting the window right after initial quotes are set

    # ── Liquidity Gate ────────────────────────────────────────
    require_two_sided: bool = True  # require both bid AND ask to be present
    min_total_shares: int = 5       # minimum totalShares on the contract

    # ── Sizing ────────────────────────────────────────────────
    sizing_mode: str = "flat"       # "flat" or "kelly"
    flat_amount_usd: float = 10.0   # USD per trade (flat mode)
    kelly_fraction: float = 0.25    # quarter-Kelly by default (kelly mode)
    total_capital_usd: float = 100.0

    # ── Exposure Limits ───────────────────────────────────────
    max_open_positions: int = 5     # max simultaneously held positions
    one_per_instrument: bool = True # no doubling into same instrument

    # ── Calibration ───────────────────────────────────────────
    params_path: str = "data/deribit/bates_params_implied.json"
    calibration_interval_hours: float = 24.0  # recalibrate every N hours
    # Trading pauses during recalibration

    # ── State & Logging ───────────────────────────────────────
    positions_path: str = "data/strategy/positions.json"
    trades_log_path: str = "data/strategy/trades.csv"

    # ── Loop ──────────────────────────────────────────────────
    poll_interval_seconds: float = 5.0
```

### `strategy/signal.py`

Pure function — no I/O, no API calls. Called by both the strategy runner and (eventually) the dashboard signal overlay.

```python
@dataclass
class Signal:
    instrument: str
    side: str              # "buy" or "sell"
    edge: float            # model - ask (buy) or bid - model (sell)
    model_price: float
    entry_price: float     # ask (buy) or bid (sell)
    strike: float
    expiry_dt: datetime
    T_years: float

def generate_signal(
    instrument: str,
    model_price: float,
    bid: float | None,
    ask: float | None,
    strike: float,
    expiry_dt: datetime,
    T_years: float,
    cfg: StrategyConfig,
) -> Signal | None:
    """
    Returns a Signal if all conditions are met, else None.

    Conditions:
      1. cfg.model_min < model_price < cfg.model_max
      2. T_years <= cfg.max_t_days / 365.25
      3. Two-sided market (both bid and ask present, if require_two_sided)
      4. BUY: model > ask + cfg.min_edge
         SELL: bid > model + cfg.min_edge
    """
```

### `strategy/sizing.py`

```python
def flat_size(cfg: StrategyConfig, entry_price: float) -> int:
    """
    Returns number of contracts for a flat USD-per-trade allocation.
    contracts = floor(flat_amount_usd / entry_price)
    Minimum 1 contract.
    """

def kelly_size(
    cfg: StrategyConfig,
    model_price: float,
    entry_price: float,
    current_portfolio_value: float,
) -> int:
    """
    Quarter-Kelly sizing for a binary option.

    Kelly fraction: f* = p - (1-p) / b
      where p = model_price (probability of winning)
            b = (1 - entry_price) / entry_price (net odds on a $1 payout)

    Allocated USD = kelly_fraction * f* * current_portfolio_value
    Contracts = floor(allocated_usd / entry_price)
    Capped at: floor(total_capital_usd * 0.20 / entry_price) per position (20% cap)
    """
```

### `strategy/execution.py`

Gemini REST client. All private endpoints use HMAC-SHA384 signed requests.

```python
class GeminiClient:
    def __init__(self, api_key: str, api_secret: str, sandbox: bool = False)

    # ── Private: trading ─────────────────────────────────────
    def place_order(
        self,
        symbol: str,        # instrument symbol e.g. GEMI-BTC2603062200-HI69000
        side: str,          # "buy" or "sell"
        outcome: str,       # "yes" (always "yes" for HI contracts)
        quantity: int,
        price: float,       # limit price (ask for buys, bid for sells)
        time_in_force: str = "immediate-or-cancel",
    ) -> dict | None        # returns order response or None on failure

    def cancel_order(self, order_id: int) -> bool

    # ── Private: account state ────────────────────────────────
    def get_positions(self) -> list[dict]
    def get_active_orders(self, symbol: str | None = None) -> list[dict]
    def get_order_history(self, status: str = "filled", limit: int = 50) -> list[dict]

    # ── Public ────────────────────────────────────────────────
    def get_spot_price(self) -> float | None
    def get_active_events(self) -> list[dict]
    def get_order_book(self, symbol: str) -> dict | None
```

### `strategy/position_log.py`

JSON file at `data/strategy/positions.json`. Loaded on startup to recover state across restarts.

```python
@dataclass
class Position:
    instrument: str         # GEMI-BTC...
    event_ticker: str       # BTC2603062200
    side: str               # "buy" or "sell"
    outcome: str            # "yes"
    quantity: int
    entry_price: float      # price paid / received
    entry_model_price: float # model price at entry (for stop-loss tracking, Phase 2)
    entry_time: str         # ISO 8601 UTC
    expiry_time: str        # ISO 8601 UTC
    order_id: int           # Gemini order ID
    settlement_index: str   # KK_BRR_BTCUSD or GRR-KAIKO_BTCUSD_1S
    status: str             # "open", "settled"
    settlement_outcome: int | None  # 0 or 1, filled after expiry

class PositionLog:
    def load(self) -> list[Position]
    def add(self, position: Position) -> None
    def update_settled(self, instrument: str, outcome: int) -> None
    def open_positions(self) -> list[Position]
    def total_exposure_usd(self) -> float
```

### `strategy/runner.py`

Main loop. Orchestrates everything.

```python
def run(cfg: StrategyConfig):
    """
    Strategy loop:

    STARTUP:
      1. Load open positions from positions.json
      2. Calibrate Bates params (fetch Deribit + run optimizer)
      3. Record calibration timestamp

    MAIN LOOP (every poll_interval_seconds):
      a. Recalibration check:
           If time_since_last_calibration >= calibration_interval_hours:
             - Log "pausing for recalibration"
             - Re-run calibration (blocks, ~2 min)
             - Update params + timestamp
             - Log "resuming"

      b. Fetch live data:
           - BTC spot price
           - Active events + contracts (bid/ask/totalShares per contract)
           - Settlement index per event (cached)

      c. Check settled positions:
           For each open position past expiry:
             - Look up outcome from settlements API
             - Update position_log, append to trades.csv
             - Log PnL

      d. Evaluate signals:
           For each active contract:
             - Skip if already holding this instrument (one_per_instrument)
             - Skip if max_open_positions reached
             - Skip if T > max_t_days
             - Skip if totalShares < min_total_shares
             - Skip if not two-sided (if require_two_sided)
             - Compute model price (Bates COS)
             - generate_signal(...)
             - If signal: compute size, place IOC limit order, log position

      e. Sleep poll_interval_seconds
    """
```

---

## Signal Logic (Key Lessons Encoded)

**Edge = model vs tradeable price, not mid:**
```
BUY edge  = model_price - ask   (only positive when model > ask)
SELL edge = bid - model_price   (only positive when bid > model)
Inside spread → no realizable edge, no trade
```

**Settlement index comes from event description text:**
The `KK_BRR_BTCUSD` vs `GRR-KAIKO_BTCUSD_1S` index is NOT determined by expiry time alone (a contract expiring at 22:00 UTC was observed using KK_BRR, not the expected 16:00 UTC rule). The index must be parsed from the contract description field in the event detail API response, and cached per event ticker.

---

## Trade Logging (`data/strategy/trades.csv`)

Append-only. One row per completed trade (entry + settlement known).

```
instrument, event_ticker, settlement_index, side, outcome_field,
quantity, entry_price, entry_model_price, entry_time, expiry_time,
settlement_outcome, pnl_per_contract, total_pnl, sizing_mode, edge_at_entry
```

`pnl_per_contract`:
- Buy: `settlement_outcome - entry_price` (e.g. paid 0.72, settled at 1 → +0.28)
- Sell: `entry_price - settlement_outcome` (e.g. received 0.78, settled at 0 → +0.78)

---

## Phase 2: Early Exit

Not implemented initially. Design placeholder:

- Each polling cycle, re-price open positions with current spot + params
- If `|current_model - entry_model| > exit_threshold` (configurable), attempt to exit
- Exit by placing IOC limit on the opposite side
- If not filled, log the attempt and continue holding
- `exit_threshold` defaults to 0.15 (configurable)

This requires the position log to store `entry_model_price`, which is already included above.

---

## Pre-Launch Checklist

1. [ ] Create REST API key in Gemini UI with NewOrder + CancelOrder permissions
2. [ ] Accept Prediction Markets Terms of Service in Gemini UI
3. [ ] Set `GEMINI_API_KEY` and `GEMINI_API_SECRET` environment variables
4. [ ] Run `python src/test_gemini_api.py` — verify all tests pass
5. [ ] Run implied calibration: `python src/calibration/implied.py`
6. [ ] Verify `data/deribit/bates_params_implied.json` exists
7. [ ] Set `flat_amount_usd` in config to desired per-trade amount
8. [ ] Start with `sizing_mode="flat"` until Kelly sizing is validated
9. [ ] Review `data/strategy/positions.json` on each restart to confirm state recovery
