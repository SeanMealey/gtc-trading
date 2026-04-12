from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from strategy.trade_ledger import LEDGER_FIELDS, LedgerRow, TradeLedger


class TradeLedgerTests(unittest.TestCase):
    def test_appends_with_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trades.csv")
            ledger = TradeLedger(path)
            ledger.append(
                LedgerRow(
                    client_order_id="cid-1",
                    instrument="GEMI-BTC2604110000-HI100000",
                    side="buy",
                    requested_quantity=10,
                    requested_price=0.42,
                    filled_quantity=10,
                    avg_fill_price=0.41,
                    model_price=0.5,
                    edge_at_decision=0.09,
                    status="filled",
                )
            )
            ledger.append(
                LedgerRow(
                    client_order_id="cid-2",
                    instrument="GEMI-BTC2604120000-HI100000",
                    side="sell",
                    requested_quantity=5,
                    requested_price=0.55,
                    status="dry",
                )
            )

            with open(path) as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 2)
            self.assertEqual(set(rows[0].keys()), set(LEDGER_FIELDS))
            self.assertEqual(rows[0]["client_order_id"], "cid-1")
            self.assertEqual(rows[0]["filled_quantity"], "10")
            self.assertEqual(rows[1]["status"], "dry")
            self.assertNotEqual(rows[0]["timestamp"], "")

    def test_read_all_returns_dicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trades.csv")
            ledger = TradeLedger(path)
            ledger.append(LedgerRow(client_order_id="x"))
            self.assertEqual(ledger.read_all()[0]["client_order_id"], "x")

    def test_read_all_empty_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TradeLedger(os.path.join(tmp, "missing.csv"))
            self.assertEqual(ledger.read_all(), [])


if __name__ == "__main__":
    unittest.main()
