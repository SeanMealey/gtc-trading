"""
Build a historical Bates parameter series from timestamped Deribit chain snapshots.

Workflow:
  1. Scan data/deribit for btc_options_chain_YYYYMMDD_HHMMSS.csv files
  2. Keep one snapshot per UTC day (latest snapshot that day)
  3. Calibrate one BatesParams JSON per day into data/deribit/params_history/
  4. Skip existing outputs unless --force is given
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(THIS_DIR, "../.."))
DERIBIT_DIR = os.path.join(ROOT, "data", "deribit")
DEFAULT_OUTPUT_DIR = os.path.join(DERIBIT_DIR, "params_history")
CHAIN_RE = re.compile(r"^btc_options_chain_(\d{8}_\d{6})\.csv$")

sys.path.insert(0, THIS_DIR)
from implied import calibrate  # noqa: E402
from implied_historical import calibrate_historical  # noqa: E402


@dataclass(frozen=True)
class SnapshotFile:
    chain_path: str
    snapshot_dt: datetime

    @property
    def day_key(self) -> str:
        return self.snapshot_dt.strftime("%Y-%m-%d")

    @property
    def output_name(self) -> str:
        return f"bates_params_{self.snapshot_dt.strftime('%Y%m%d_%H%M%S')}.json"


def discover_daily_snapshots(deribit_dir: str) -> list[SnapshotFile]:
    snapshots: list[SnapshotFile] = []
    for name in sorted(os.listdir(deribit_dir)):
        match = CHAIN_RE.match(name)
        if not match:
            continue
        snapshot_dt = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
        snapshots.append(
            SnapshotFile(
                chain_path=os.path.join(deribit_dir, name),
                snapshot_dt=snapshot_dt,
            )
        )

    latest_per_day: dict[str, SnapshotFile] = {}
    for snapshot in snapshots:
        current = latest_per_day.get(snapshot.day_key)
        if current is None or snapshot.snapshot_dt > current.snapshot_dt:
            latest_per_day[snapshot.day_key] = snapshot

    return sorted(latest_per_day.values(), key=lambda item: item.snapshot_dt)


def progress_bar(index: int, total: int, label: str, width: int = 28) -> str:
    if total <= 0:
        total = 1
    filled = int(width * index / total)
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {index}/{total} {label}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily historical Bates params")
    parser.add_argument("--deribit-dir", default=DERIBIT_DIR, help="Directory with Deribit chain snapshots")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory to write params_history JSON files")
    parser.add_argument("--force", action="store_true", help="Recompute outputs even if the JSON already exists")
    parser.add_argument("--r", type=float, default=0.05, help="Risk-free rate")
    parser.add_argument("--min-oi", type=float, default=10.0, help="Min open interest (BTC)")
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=1.0,
        help="Min trade-based liquidity proxy used by the historical calibrator",
    )
    parser.add_argument(
        "--min-mark-price-btc",
        type=float,
        default=0.001,
        help="Min option mark price in BTC used by the historical calibrator",
    )
    parser.add_argument(
        "--min-iv-pct",
        type=float,
        default=1.0,
        help="Min IV percent used by the historical calibrator",
    )
    parser.add_argument(
        "--max-iv-pct",
        type=float,
        default=300.0,
        help="Max IV percent used by the historical calibrator",
    )
    parser.add_argument("--min-tte", type=float, default=3.0, help="Min time to expiry (days)")
    parser.add_argument("--N", type=int, default=64, help="COS terms")
    parser.add_argument("--de-maxiter", type=int, default=60, help="Historical DE max iterations")
    parser.add_argument("--de-popsize", type=int, default=6, help="Historical DE population size")
    parser.add_argument("--polish-maxiter", type=int, default=300, help="Historical L-BFGS-B max iterations")
    parser.add_argument(
        "--calibrator",
        choices=["live", "historical"],
        default="live",
        help="Which calibrator to use for the discovered chain files",
    )
    args = parser.parse_args()

    snapshots = discover_daily_snapshots(args.deribit_dir)
    os.makedirs(args.output_dir, exist_ok=True)

    if not snapshots:
        raise SystemExit(f"No timestamped chain snapshots found in {args.deribit_dir}")

    print(f"Found {len(snapshots)} daily snapshot(s) for calibration.")

    completed = 0
    skipped = 0
    failed: list[tuple[str, str]] = []
    total = len(snapshots)

    for idx, snapshot in enumerate(snapshots, start=1):
        output_path = os.path.join(args.output_dir, snapshot.output_name)
        label = snapshot.snapshot_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        print(progress_bar(idx, total, label))

        if os.path.exists(output_path) and not args.force:
            skipped += 1
            print(f"  skip: {output_path} already exists")
            continue

        try:
            if args.calibrator == "historical":
                calibrate_historical(
                    chain_path=snapshot.chain_path,
                    output_path=output_path,
                    r=args.r,
                    min_liquidity=args.min_liquidity,
                    min_tte_days=args.min_tte,
                    min_mark_price_btc=args.min_mark_price_btc,
                    min_iv_pct=args.min_iv_pct,
                    max_iv_pct=args.max_iv_pct,
                    N_cos=args.N,
                    de_maxiter=args.de_maxiter,
                    de_popsize=args.de_popsize,
                    polish_maxiter=args.polish_maxiter,
                )
            else:
                calibrate(
                    chain_path=snapshot.chain_path,
                    r=args.r,
                    min_oi=args.min_oi,
                    min_tte_days=args.min_tte,
                    N_cos=args.N,
                    output_path=output_path,
                )
        except Exception as exc:
            failed.append((snapshot.snapshot_dt.strftime("%Y-%m-%d %H:%M:%S UTC"), str(exc)))
            print(f"  fail: {exc}")
            continue

        completed += 1
        print(f"  saved: {output_path}")

    print("\nDone.")
    print(f"  snapshots discovered: {total}")
    print(f"  calibrated:          {completed}")
    print(f"  skipped:             {skipped}")
    print(f"  failed:              {len(failed)}")
    print(f"  output dir:          {args.output_dir}")
    if failed:
        print("\nFailed snapshots:")
        for label, error in failed:
            print(f"  {label} | {error}")


if __name__ == "__main__":
    main()
