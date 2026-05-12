# Gemini Prediction Markets — WebSocket & Fast API

> llms.txt reference for building against Gemini's real-time prediction markets APIs.
> Sources: docs.gemini.com (April 2026). Always check the official docs for the latest.

## Overview

Gemini offers **both WebSocket and Fast API** access for prediction markets — not just REST. The recommended path for new integrations is **Fast API**, their next-generation low-latency WebSocket. The original WebSocket API (`wss://ws.gemini.com`) is also fully supported and is what the prediction markets getting-started guide uses directly.

| API | Endpoint | Latency | Use Case |
|-----|----------|---------|----------|
| REST | `https://api.gemini.com/v1/prediction-markets/...` | Standard | Browse markets, check positions |
| WebSocket | `wss://ws.gemini.com` | Real-time | Stream prices, place/cancel orders, order events |
| Fast API | `wss://wsapi.fast.gemini.com` | Sub-10ms (p99~5–15ms) | Same as WS but lower latency, unified trading + market data |

## How Prediction Markets Work

- **Events** are real-world questions (e.g. "2028 Presidential Election Winner").
- Each event has **contracts** (possible outcomes you can trade).
- Every contract has a **YES** and **NO** side. Price ≈ implied probability.
- Payout is always **$1** for winners, **$0** for losers. Settlement is automatic.
- Prices are between **$0.01–$0.99**. YES + NO ≈ $1.00 (spread accounts for difference).

## Ticker Format

Trading symbols use the `instrumentSymbol` field from the REST events endpoint.

Example: `GEMI-PRES2028-VANCE`, `GEMI-FEDJAN26-DN25`, `GEMI-BTC100K-YES`

## Authentication (WebSocket)

Authentication headers **must** be sent during the initial WebSocket handshake. You **cannot** authenticate after connecting. Market data streams work without auth; trading and order events require it.

### Requirements
- API key must be **account-scoped**
- Must have **time-based nonce** enabled
- Must have **Trading** permission (grants `NewOrder` and `CancelOrder`)
- Accept Prediction Markets terms of service in the Gemini Exchange UI first

### Signature Algorithm

```
nonce = current_epoch_time_in_seconds (as string)
payload = base64_encode(nonce)
signature = hex(hmac_sha384(payload, api_secret))
```

### Headers (set during WS handshake)

```
X-GEMINI-APIKEY: <your-api-key>
X-GEMINI-NONCE: <nonce>
X-GEMINI-PAYLOAD: <payload>
X-GEMINI-SIGNATURE: <signature>
```

### Node.js Example

```javascript
const crypto = require("crypto");
const WebSocket = require("ws");

const nonce = Math.floor(Date.now() / 1000).toString();
const payload = Buffer.from(nonce).toString("base64");
const signature = crypto
  .createHmac("sha384", API_SECRET)
  .update(payload)
  .digest("hex");

const ws = new WebSocket("wss://ws.gemini.com", {
  headers: {
    "X-GEMINI-APIKEY": API_KEY,
    "X-GEMINI-NONCE": nonce,
    "X-GEMINI-PAYLOAD": payload,
    "X-GEMINI-SIGNATURE": signature,
  },
});
```

## Message Format (Fast API / WebSocket)

### Request

```json
{
  "id": "1",
  "method": "METHOD_NAME",
  "params": { ... }
}
```

### Success Response

```json
{
  "id": "1",
  "status": 200,
  "result": { ... }
}
```

### Error Response

```json
{
  "id": "1",
  "status": 401,
  "error": {
    "code": -1002,
    "msg": "Authentication required"
  }
}
```

## Methods

### subscribe / unsubscribe

```json
{
  "id": "1",
  "method": "subscribe",
  "params": ["GEMI-PRES2028-VANCE@bookTicker"]
}
```

### order.place

Place a prediction market limit order via WebSocket:

```json
{
  "id": "2",
  "method": "order.place",
  "params": {
    "symbol": "GEMI-PRES2028-VANCE",
    "side": "BUY",
    "type": "LIMIT",
    "timeInForce": "GTC",
    "price": "0.27",
    "quantity": "10",
    "eventOutcome": "YES",
    "clientOrderId": "my-order-id"
  }
}
```

| Param | Values | Notes |
|-------|--------|-------|
| `side` | `BUY`, `SELL` | |
| `type` | `LIMIT` | Only limit orders currently supported |
| `timeInForce` | `GTC` | Good Til Cancelled |
| `price` | `"0.01"` – `"0.99"` | String, the contract price |
| `quantity` | `> 0` | Number of contracts (string) |
| `eventOutcome` | `YES`, `NO` | Which side of the contract |
| `clientOrderId` | any string | Optional, for your tracking |

### order.cancel

```json
{
  "id": "3",
  "method": "order.cancel",
  "params": { "orderId": "73797746498585286" }
}
```

### order.cancel_all / order.cancel_session

Cancel all orders or just orders from the current session.

### Utility Methods

- `ping` — keepalive
- `time` — server time
- `conninfo` — connection info
- `list_subscriptions` — list active subscriptions
- `depth` — request L2 order book snapshot

## Streams

All streams use the pattern `{symbol}@streamName`. For prediction markets, `{symbol}` is the `instrumentSymbol` (e.g. `GEMI-PRES2028-VANCE`).

### bookTicker (real-time best bid/ask)

Subscribe: `GEMI-PRES2028-VANCE@bookTicker`

```json
{
  "u": 1751505576085,
  "E": 1751508438600117161,
  "s": "GEMI-PRES2028-VANCE",
  "b": "0.26",
  "B": "5000",
  "a": "0.28",
  "A": "3200"
}
```

| Field | Description |
|-------|-------------|
| `u` | Update ID |
| `E` | Event time (nanoseconds) |
| `s` | Symbol |
| `b` | Best bid price |
| `B` | Best bid quantity |
| `a` | Best ask price |
| `A` | Best ask quantity |

### Depth Streams (L2 order book)

| Stream | Frequency |
|--------|-----------|
| `{symbol}@depth5` | Top 5 levels, 1s |
| `{symbol}@depth10` | Top 10 levels, 1s |
| `{symbol}@depth20` | Top 20 levels, 1s |
| `{symbol}@depth5@100ms` | Top 5 levels, 100ms |
| `{symbol}@depth10@100ms` | Top 10 levels, 100ms |
| `{symbol}@depth20@100ms` | Top 20 levels, 100ms |
| `{symbol}@depth` | Differential, 1s |
| `{symbol}@depth@100ms` | Differential, 100ms |

Partial depth response:

```json
{
  "lastUpdateId": 12345678,
  "bids": [["0.26", "5000"], ["0.25", "3000"]],
  "asks": [["0.28", "3200"], ["0.29", "1500"]]
}
```

Differential depth response (quantity `"0"` = level removed):

```json
{
  "e": "depthUpdate",
  "E": 1751508260659505382,
  "s": "GEMI-PRES2028-VANCE",
  "U": 12345677,
  "u": 12345678,
  "b": [["0.26", "4800"]],
  "a": [["0.28", "3200"]]
}
```

### Trade Stream

Subscribe: `{symbol}@trade`

```json
{
  "E": 1759873803503023900,
  "s": "GEMI-PRES2028-VANCE",
  "t": 2840140956529623,
  "p": "0.27",
  "q": "100",
  "m": true
}
```

| Field | Description |
|-------|-------------|
| `t` | Trade ID |
| `p` | Price |
| `q` | Quantity |
| `m` | Is buyer the maker |

### orders@account (requires auth)

Real-time order lifecycle events for your account.

```json
{
  "E": 1759291847686856569,
  "s": "GEMI-PRES2028-VANCE",
  "i": 73797746498585286,
  "c": "my-order-id",
  "S": "BUY",
  "o": "LIMIT",
  "X": "NEW",
  "p": "0.27",
  "q": "10",
  "z": "10",
  "O": "YES",
  "T": 1759291847686856569
}
```

| Field | Description |
|-------|-------------|
| `i` | Order ID |
| `c` | Client order ID |
| `S` | Side: `BUY` / `SELL` |
| `X` | Status: `NEW`, `OPEN`, `FILLED`, `PARTIALLY_FILLED`, `CANCELED`, `REJECTED`, `MODIFIED` |
| `p` | Order price |
| `q` | Original quantity |
| `z` | Remaining quantity |
| `Z` | Executed quantity (last fill for FILLED events; cumulative for CANCELED) |
| `L` | Last execution price |
| `O` | Event outcome: `YES` / `NO` |
| `r` | Rejection/cancellation reason (when applicable) |

### balances@account (requires auth)

Real-time balance updates. Also available as `balances@account@1s` for periodic snapshots.

```json
{
  "e": "balanceUpdate",
  "E": 1768250434780,
  "u": 1768250421600,
  "B": [{ "a": "USD", "f": "207.39" }]
}
```

## REST Endpoints (Prediction Markets)

These are public (no auth) unless noted.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v1/prediction-markets/events` | No | List events (filter by `status`, `category`, `search`) |
| GET | `/v1/prediction-markets/events/{ticker}` | No | Single event details |
| GET | `/v1/prediction-markets/events/recently-settled` | No | Last 24h settled events |
| GET | `/v1/prediction-markets/events/newly-listed` | No | Newly listed events |
| GET | `/v1/prediction-markets/events/upcoming` | No | Upcoming events |
| GET | `/v1/prediction-markets/categories` | No | Available categories |
| POST | `/v1/prediction-markets/order` | Yes | Place limit order |
| POST | `/v1/prediction-markets/order/cancel` | Yes | Cancel order |
| POST | `/v1/prediction-markets/orders/active` | Yes | List open orders |
| POST | `/v1/prediction-markets/orders/history` | Yes | Order history |
| POST | `/v1/prediction-markets/positions` | Yes | Current positions |

## Fast API Performance Tiers

| Tier | Latency (p99) | Access |
|------|---------------|--------|
| Tier 2 (Public Internet) | ~15ms | Default, connect to `wss://wsapi.fast.gemini.com` |
| Tier 1 (In-Region AWS us-east-1) | ~10ms | Requires onboarding |
| Tier 0 (Local Zone, near NY5) | ~5ms | Requires onboarding |

Contact your account manager for Tier 0/1 access.

## Common Errors

| Code | Message | Fix |
|------|---------|-----|
| `-2010` | Order rejected | Price must be $0.01–$0.99, qty > 0, outcome must be YES/NO. Market may be closed. |
| `-1002` | Authentication required | Trading methods need auth headers at handshake time. |
| `403` / `TERMS_NOT_ACCEPTED` | Terms not accepted | Accept Prediction Markets terms in the Gemini Exchange UI. |
| `InsufficientFunds` | Insufficient funds | Need enough USD. 100 contracts at $0.27 = $27. |

## Quick Reference: Full Working Example (Node.js)

Stream prices + place an order + receive fill notifications:

```javascript
const crypto = require("crypto");
const WebSocket = require("ws");

const API_KEY = "your-api-key";
const API_SECRET = "your-api-secret";
const SYMBOL = "GEMI-PRES2028-VANCE";

const nonce = Math.floor(Date.now() / 1000).toString();
const payload = Buffer.from(nonce).toString("base64");
const signature = crypto
  .createHmac("sha384", API_SECRET)
  .update(payload)
  .digest("hex");

const ws = new WebSocket("wss://ws.gemini.com", {
  headers: {
    "X-GEMINI-APIKEY": API_KEY,
    "X-GEMINI-NONCE": nonce,
    "X-GEMINI-PAYLOAD": payload,
    "X-GEMINI-SIGNATURE": signature,
  },
});

ws.on("open", () => {
  ws.send(JSON.stringify({
    id: "1",
    method: "subscribe",
    params: [
      `${SYMBOL}@bookTicker`,
      "orders@account",
      "balances@account",
    ],
  }));
});

let orderPlaced = false;

ws.on("message", (raw) => {
  const data = JSON.parse(raw);

  // Stream prices
  if (data.b && data.a && data.s === SYMBOL) {
    console.log(`${data.s}  bid: $${data.b}  ask: $${data.a}`);

    // Place order on first price update
    if (!orderPlaced) {
      orderPlaced = true;
      ws.send(JSON.stringify({
        id: "2",
        method: "order.place",
        params: {
          symbol: SYMBOL,
          side: "BUY",
          type: "LIMIT",
          timeInForce: "GTC",
          price: "0.27",
          quantity: "10",
          eventOutcome: "YES",
          clientOrderId: `order-${Date.now()}`,
        },
      }));
    }
  }

  // Order updates
  if (data.X) {
    console.log(`Order ${data.X}: ${data.S} ${data.O} @ $${data.p} x${data.q}`);
  }

  // Balance updates
  if (data.e === "balanceUpdate") {
    data.B.forEach((b) => console.log(`Balance: ${b.a} = ${b.f}`));
  }
});
```

## Links

- Getting Started: https://docs.gemini.com/prediction-markets/getting-started
- Fast API Intro: https://docs.gemini.com/websocket/fast-api/introduction
- Fast API Streams: https://docs.gemini.com/websocket/fast-api/streams
- Fast API Message Format: https://docs.gemini.com/websocket/fast-api/message-format
- REST Markets API: https://docs.gemini.com/prediction-markets/markets
- REST Trading API: https://docs.gemini.com/prediction-markets/trading
- REST Positions API: https://docs.gemini.com/prediction-markets/positions
- Changelog: https://docs.gemini.com/changelog/revision-history
