# Live Runner Deployment

The live market maker runs as a single long-lived Python process on a Linux host. This document is the runbook for setting it up from scratch and rolling it out safely.

The runner now refreshes the Deribit BTC options chain and re-runs implied Bates calibration on its own cadence while it is live. That cadence is controlled by `calibration_interval_hours` in `config/live.json`.

## Layout on the host

```
/home/ubuntu/gtc-trading/            # checkout root
  src/
  config/live.json                   # live config (no paper-only fields)
  data/strategy/positions.live.json  # local position state
  data/strategy/trades.live.csv      # append-only trade ledger
  logs/live_runner.log               # rotating runner log
  logs/KILL_SWITCH                   # touch this file to halt the runner
  .venv/                             # python venv with deploy/requirements-live.txt installed

/etc/gtc-trading/live.env            # holds GEMINI_API_KEY / GEMINI_API_SECRET (chmod 600)
```

## One-time setup

1. **Create config dir**
   ```
   sudo mkdir -p /etc/gtc-trading
   ```

2. **Clone the repo into `/home/ubuntu/gtc-trading`** and copy `config/live.json` from this repo.

3. **Build the C++ pricer extension on the target host** — the macOS `.so` shipped in the repo will not load on Linux.
   ```
   cd /home/ubuntu/gtc-trading/src/pricer
   ./build.sh
   ```

4. **Create the venv and install live deps**
   ```
   cd /home/ubuntu/gtc-trading
   python3 -m venv .venv
   .venv/bin/pip install -r deploy/requirements-live.txt
   ```
   This environment now includes `pandas`, `scipy`, and `websockets` because live auto-calibration runs in-process and the spot feed uses Gemini Fast API over websocket.

5. **Provision Gemini credentials**
   ```
   sudo install -m 600 /dev/stdin /etc/gtc-trading/live.env <<EOF
   GEMINI_API_KEY=account-xxxxxxxxxxxxxxxxxxxx
   GEMINI_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   EOF
   sudo chown ubuntu:ubuntu /etc/gtc-trading/live.env
   ```
   Required Gemini permissions: **Fund Management + Trading (NewOrder + CancelOrder)**.
   You also need to accept the Prediction Markets ToS in the Gemini UI before any private PM endpoint will return data.

6. **Install the systemd unit**
   ```
   sudo install -m 644 deploy/live-runner.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

## Rollout sequence

Do these in order. Do not skip ahead.

### 1. Fast API connectivity probe

`config/live.json` now points at the provisioned Fast API endpoint. Validate websocket connectivity from the EC2 instance:

```
cd /opt/gtc-trading
sudo -u trader .venv/bin/python src/test_gemini_fast_api.py
```

Expected: one JSON message from the `btcusd@bookTicker` stream. This mirrors:

```
echo '{"id":"1","method":"subscribe","params":["btcusd@bookTicker"]}' | \
  websocat -n 'ws://2e4e907b.fast.gemini.com'
```

### 2. Auth probe (no orders)

```
cd /home/ubuntu/gtc-trading
sudo -u ubuntu \
  GEMINI_API_KEY=$(grep ^GEMINI_API_KEY /etc/gtc-trading/live.env | cut -d= -f2) \
  GEMINI_API_SECRET=$(grep ^GEMINI_API_SECRET /etc/gtc-trading/live.env | cut -d= -f2) \
  .venv/bin/python src/test_gemini_api.py
```

Expected: `[OK]` lines for positions, active orders, and order history. If any fail, fix credentials / permissions before continuing.

### 3. Dry-run smoke test

`config/live.json` ships with `submit_orders=false` and `dry_run=true`. Run a single tick:

```
sudo -u ubuntu .venv/bin/python -m strategy.runner --config config/live.json --once
```

Expected: a heartbeat line and `DRY ...` lines showing what *would* have been submitted, plus rows in `data/strategy/trades.live.csv` with `status=dry`. The runner must reach the end of the tick without raising — if it does, fix the cause before enabling submission.

With auto-calibration enabled, you should also see log lines showing the Deribit snapshot refresh and parameter update whenever the refresh cadence is due.

### 4. Reconciliation gate

Edit `config/live.json` and confirm `require_state_reconciliation: true`. Restart the dry run. The preflight log line should read:

```
reconciliation OK: 0 local positions, 0 remote positions, 0 active orders
```

If you have manual positions on the account, either close them, copy them into `positions.live.json`, or set `reconciliation_max_quantity_drift` to the exact gap and re-run.

### 5. Enable order submission for one tiny order

Edit `config/live.json`:

```json
"submit_orders": true,
"dry_run": false,
"max_notional_per_order_usd": 1.0,
"max_total_notional_usd": 5.0,
"daily_filled_notional_cap_usd": 5.0
```

Start the service:

```
sudo systemctl start live-runner
journalctl -u live-runner -f
```

Watch for one order submission and verify on the Gemini UI that the position or resting order appears. With the Fast API websocket path documented in `gemini-prediction-markets-llms.md`, the live config now uses `time_in_force: "GTC"` for prediction-market order entry.

### 6. Widen limits

Once you have at least one confirmed live fill, ratchet `max_notional_per_order_usd`, `max_total_notional_usd`, and `daily_filled_notional_cap_usd` upward in small increments. Restart the service after each edit.

```
sudo systemctl restart live-runner
```

## Operational controls

* **Kill switch**: `sudo -u ubuntu touch /home/ubuntu/gtc-trading/logs/KILL_SWITCH`. Runner stops at the next tick. Remove the file and restart to resume.
* **Stop / start**: `sudo systemctl stop live-runner` / `sudo systemctl start live-runner`.
* **Logs**: `journalctl -u live-runner -f` or `tail -f logs/live_runner.log`.
* **Trade ledger**: `data/strategy/trades.live.csv` — every order attempt, fill, error, and decision context.
* **Calibration cadence**: set `calibration_interval_hours` in `config/live.json`. For every 10 minutes, use `0.1666667`.

## Circuit breakers (automatic halt conditions)

The runner opens its internal circuit and stops trading (without crashing) when any of the following hit:

* `max_consecutive_api_failures` consecutive Gemini errors
* `daily_loss_limit_usd` realised loss for the day
* `daily_filled_notional_cap_usd` filled notional for the day

To resume after a circuit-open event, investigate the cause in the log, then restart the service.

## Recovery

If the runner exits and restarts mid-day, the preflight reconciles `positions.live.json` against Gemini. A drift greater than `reconciliation_max_quantity_drift` aborts startup. Reconcile manually before restarting.

## Known residual risks

* Prediction-market order entry now uses Gemini Fast API websocket methods (`order.place`, `order.cancel`) and authenticated `orders@account` events. Snapshot-style account reads such as positions, active orders, and order history still use the documented REST endpoints because the downloaded PM Fast API doc did not provide websocket snapshot methods for those resources.
* Partial-fill / fee fields should still be cross-checked against live Gemini behaviour on the first tiny order. The runner records the order-placement response plus any cached `orders@account` event seen immediately after placement.
* Settlement / outcome polling is *not* yet automated — open positions remain `open` until you close or settle them manually. Add a settlement poller before running over expiries.
