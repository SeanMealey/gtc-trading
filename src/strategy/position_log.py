"""
Persistence for strategy positions.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass


@dataclass
class Position:
    instrument: str
    event_ticker: str
    side: str
    outcome: str
    quantity: int
    entry_price: float
    entry_model_price: float
    edge_at_entry: float
    entry_time: str
    expiry_time: str
    order_id: str
    settlement_index: str
    status: str
    spot_at_entry: float | None = None
    bid_at_entry: float | None = None
    ask_at_entry: float | None = None
    bid_size_at_entry: float = 0.0
    ask_size_at_entry: float = 0.0
    settlement_outcome: int | None = None
    settlement_time: str = ""
    params_path: str = ""


class PositionLog:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> list[Position]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as handle:
            raw = json.load(handle)
        return [Position(**item) for item in raw]

    def save(self, positions: list[Position]) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w") as handle:
            json.dump([asdict(p) for p in positions], handle, indent=2)

    def add(self, position: Position) -> None:
        positions = self.load()
        positions.append(position)
        self.save(positions)

    def update_settled(
        self,
        instrument: str,
        outcome: int,
        settlement_time: str,
    ) -> Position | None:
        positions = self.load()
        updated = None
        for idx, position in enumerate(positions):
            if position.instrument == instrument and position.status == "open":
                position.status = "settled"
                position.settlement_outcome = int(outcome)
                position.settlement_time = settlement_time
                positions[idx] = position
                updated = position
                break
        if updated is not None:
            self.save(positions)
        return updated

    def open_positions(self) -> list[Position]:
        return [position for position in self.load() if position.status == "open"]

    def total_exposure_usd(self) -> float:
        exposure = 0.0
        for position in self.open_positions():
            if position.side == "buy":
                exposure += position.quantity * position.entry_price
            else:
                exposure += position.quantity * (1.0 - position.entry_price)
        return exposure
