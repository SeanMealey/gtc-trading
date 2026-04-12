"""
Append-only CSV ledger for live trades.

Each row records the full decision context behind an order so the runner's
behaviour can be audited after the fact: the model price, the executable edge,
the scenario-gate verdict, the params version, and the actual fill returned by
Gemini.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
from dataclasses import asdict, dataclass


LEDGER_FIELDS = (
    "timestamp",
    "client_order_id",
    "order_id",
    "instrument",
    "event_ticker",
    "side",
    "outcome",
    "requested_quantity",
    "requested_price",
    "filled_quantity",
    "avg_fill_price",
    "fee",
    "model_price",
    "edge_at_decision",
    "scenario_decision",
    "scenario_reasons",
    "inventory_score",
    "inventory_adjustment",
    "params_source",
    "params_calibrated_at",
    "spot_at_decision",
    "bid_at_decision",
    "ask_at_decision",
    "status",
    "notes",
)


@dataclass
class LedgerRow:
    timestamp: str = ""
    client_order_id: str = ""
    order_id: str = ""
    instrument: str = ""
    event_ticker: str = ""
    side: str = ""
    outcome: str = ""
    requested_quantity: int = 0
    requested_price: float = 0.0
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    fee: float = 0.0
    model_price: float = 0.0
    edge_at_decision: float = 0.0
    scenario_decision: str = ""
    scenario_reasons: str = ""
    inventory_score: float = 0.0
    inventory_adjustment: float = 0.0
    params_source: str = ""
    params_calibrated_at: str = ""
    spot_at_decision: float = 0.0
    bid_at_decision: float = 0.0
    ask_at_decision: float = 0.0
    status: str = ""
    notes: str = ""


class TradeLedger:
    def __init__(self, path: str):
        self.path = path

    def _ensure_header(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
                writer.writeheader()

    def append(self, row: LedgerRow) -> None:
        self._ensure_header()
        if not row.timestamp:
            row.timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        with open(self.path, "a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
            writer.writerow(asdict(row))

    def read_all(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as handle:
            return list(csv.DictReader(handle))
