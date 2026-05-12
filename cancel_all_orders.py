#!/usr/bin/env python3
"""Cancel all active resting orders on Gemini prediction markets."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


for _env_path in (Path("/etc/gtc-trading/live.env"), Path(__file__).parent / ".env"):
    _load_env_file(_env_path)

from strategy.config import StrategyConfig  # noqa: E402
from strategy.execution import GeminiExecutionClient  # noqa: E402


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/live.json"
    cfg = StrategyConfig.load(config_path)
    client = GeminiExecutionClient(
        base_url=cfg.gemini_base_url,
        fast_api_url=cfg.gemini_fast_api_url,
        reference_price_source=cfg.reference_price_source,
        kraken_ws_url=cfg.kraken_ws_url,
        kraken_symbol=cfg.kraken_symbol,
        reference_price_fallback_to_gemini=cfg.reference_price_fallback_to_gemini,
        timeout=cfg.request_timeout_seconds,
        dry_run=False,
    )
    try:
        active = client.get_active_orders(limit=500)
        print(f"found {len(active)} active orders")
        results = client.cancel_all_orders()
        print(f"cancelled {len(results)} orders")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())