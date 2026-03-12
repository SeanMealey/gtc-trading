from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd


# Default configuration constants
DEFAULT_DATA_ROOT = Path(
    r"C:data\binance\latest"
)
DEFAULT_OUTPUT_DIR = Path("data") / "crypto"
DEFAULT_START_DATE = "20250101"  # YYYYMMDD
DEFAULT_END_DATE = datetime.now().strftime("%Y%m%d")    # YYYYMMDD (inclusive)
DEFAULT_INTERVAL_MIN = 1440
DEFAULT_DATASETS = ["trade", "level1", "book"]
DEFAULT_SYMBOLS: List[str] = ["BTCUSD", "ETHUSD", "SOLUSD"]  # Crypto symbols for Binance
DEFAULT_BINANCE_MARKET = "spot-us"  # Default Binance market

TYPE_MAP: Dict[str, str] = {
    "trade": "trade_1min",
    "level1": "level1_1min",
    "book": "book_1min",
}

BINANCE_ENDPOINTS = {
    "spot": "https://api.binance.com/api/v3/klines",
    "spot-us": "https://api.binance.us/api/v3/klines",
    "usdt-futures": "https://fapi.binance.com/fapi/v1/klines",
    "coin-futures": "https://dapi.binance.com/dapi/v1/klines",
}

BINANCE_INTERVALS = {
    1: "1m",
    3: "3m",
    5: "5m",
    15: "15m",
    30: "30m",
    60: "1h",
    120: "2h",
    240: "4h",
    360: "6h",
    480: "8h",
    720: "12h",
    1440: "1d",
}

BINANCE_KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]


def normalize_binance_symbol(symbol: str, market: str) -> str:
    symbol = symbol.upper()
    if market == "coin-futures":
        if symbol == "BTCUSD":
            return "BTCUSD_PERP"
        return symbol

    if symbol == "BTCUSD":
        return "BTCUSD"
    return symbol


def fetch_binance_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    endpoint: str,
    limit: int = 1000,
) -> List[List]:
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }
    url = f"{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; DataExtractor/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 451 and "api.binance.com" in url:
            raise RuntimeError(
                "Binance spot endpoint blocked (HTTP 451). "
                "Try --binance-market spot-us or use a different network."
            ) from exc
        raise
    return json.loads(payload)


def download_binance_klines(
    symbol: str,
    start_date: str,
    end_date: str,
    interval_min: int,
    market: str,
    verbose: bool = False,
) -> pd.DataFrame:
    if interval_min not in BINANCE_INTERVALS:
        raise ValueError(
            f"Unsupported interval {interval_min}min for Binance API. "
            f"Supported: {sorted(BINANCE_INTERVALS)}"
        )

    endpoint = BINANCE_ENDPOINTS[market]
    interval = BINANCE_INTERVALS[interval_min]
    symbol = normalize_binance_symbol(symbol, market)

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d") + timedelta(days=1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    rows: List[List] = []
    cursor = start_ms
    request_count = 0
    while cursor < end_ms:
        request_count += 1
        data = fetch_binance_klines(symbol, cursor, end_ms, interval, endpoint)
        if not data:
            break
        rows.extend(data)
        last_close = data[-1][6]
        if verbose:
            progress = min(100.0, ((last_close + 1 - start_ms) / max(1, end_ms - start_ms)) * 100)
            print(
                f"[binance] {symbol} {interval} request {request_count}: "
                f"{len(rows):,} rows total, {progress:.1f}% complete"
            )
        next_cursor = last_close + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(data) < 1000:
            break

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=BINANCE_KLINE_COLUMNS)
    for col in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]:
        df[col] = df[col].astype(float)
    df["trades"] = df["trades"].astype(int)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    
    # Match import_data.ipynb format
    df = df.rename(columns={
        "open_time": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume"
    })
    
    # Add columns to match import_data.ipynb structure
    df["Ticker"] = symbol
    df["Exchange"] = "binance"
    df["Adj Close"] = df["Close"]  # No adjustment for crypto
    df["Adj Factor"] = 1.0
    df["Daily Return"] = df["Close"].pct_change()
    df["Adj Daily Return"] = df["Close"].pct_change()
    
    # Reorder columns to match import_data.ipynb
    df = df[["Ticker", "Exchange", "Date", "Open", "High", "Low", "Close", 
             "Adj Close", "Adj Factor", "Daily Return", "Adj Daily Return", "Volume"]]
    
    return df


def extract_binance_klines(
    symbols: List[str],
    start_date: str,
    end_date: str,
    interval_min: int,
    market: str,
    output_dir: Path,
    verbose: bool = True,
) -> Dict[str, any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    date_label = f"{start_date}_to_{end_date}" if start_date != end_date else start_date

    frames: List[pd.DataFrame] = []
    output_files: Dict[str, str] = {}
    for symbol in symbols:
        df = download_binance_klines(
            symbol,
            start_date,
            end_date,
            interval_min,
            market,
            verbose=verbose,
        )
        if df.empty:
            if verbose:
                print(f"[binance] No data for {symbol} ({market})")
            continue
        # Follow exchange_name.csv naming convention
        single_path = output_dir / f"binance_{symbol}.csv"
        df.to_csv(single_path, index=False)
        output_files[symbol] = str(single_path)
        frames.append(df)
        if verbose:
            print(f"[binance] Loaded {len(df):,} rows for {symbol} ({market})")
            print(f"[binance] Saved {symbol} to: {single_path}")

    if not frames:
        return {"data": pd.DataFrame(), "output_file": None, "output_files": {}}

    combined = pd.concat(frames, ignore_index=True).sort_values(["Ticker", "Date"]).reset_index(drop=True)
    output_path = output_dir / f"binance_combined.csv"
    combined.to_csv(output_path, index=False)
    if verbose:
        print(f"[binance] Saved combined to: {output_path}")

    return {"data": combined, "output_file": str(output_path), "output_files": output_files}


class DataExtractor:
    """
    A flexible data extraction class for loading and processing Binance futures market data.
    
    Usage:
        # Use with defaults
        extractor = DataExtractor()
        results = extractor.extract()
        
        # Customize parameters
        extractor = DataExtractor(
            start_date="20240101",
            end_date="20241231",
            interval_min=60,
            datasets=["trade", "level1"],
            symbols=["BTCUSDT", "ETHUSDT"]
        )
        results = extractor.extract()
    """
    
    def __init__(
        self,
        data_root: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        interval_min: Optional[int] = None,
        datasets: Optional[List[str]] = None,
        symbols: Optional[List[str]] = None,
    ):
        """
        Initialize DataExtractor with configurable parameters.
        
        Args:
            data_root: Root directory containing market data files
            output_dir: Directory to save output files
            start_date: Start date in YYYYMMDD format
            end_date: End date in YYYYMMDD format (inclusive)
            interval_min: Downsample interval in minutes
            datasets: List of datasets to load (e.g., ["trade", "level1", "book"])
            symbols: List of trading pairs (e.g., ["BTCUSDT", "ETHUSDT"]). 
                    Use None or empty list to auto-discover all available symbols.
        """
        self.data_root = data_root or DEFAULT_DATA_ROOT
        self.output_dir = output_dir or DEFAULT_OUTPUT_DIR
        self.start_date = start_date or DEFAULT_START_DATE
        self.end_date = end_date or DEFAULT_END_DATE
        self.interval_min = interval_min or DEFAULT_INTERVAL_MIN
        self.datasets = datasets or DEFAULT_DATASETS.copy()
        self.symbols = symbols if symbols is not None else DEFAULT_SYMBOLS.copy()
        
        # Validate datasets
        bad = [ds for ds in self.datasets if ds not in TYPE_MAP]
        if bad:
            raise ValueError(f"Unsupported dataset(s): {bad}. Allowed: {list(TYPE_MAP)}")
    
    def day_folder(self, dataset: str, day: str) -> Path:
        """Get the folder path for a specific dataset and day."""
        return self.data_root / TYPE_MAP[dataset] / day
    
    def discover_symbols(self, dataset: str, day: str) -> List[str]:
        """Discover all available symbols for a given dataset and day."""
        folder = self.day_folder(dataset, day)
        if not folder.exists():
            return []

        symbols: List[str] = []
        for file in folder.glob(f"*.{day}.{dataset}.1min.csv.gz"):
            # Format: SYMBOL.DAY.TYPE.1min.csv.gz
            symbols.append(file.name.split(".")[0])
        return sorted(symbols)
    
    def build_file_path(self, dataset: str, day: str, symbol: str) -> Path:
        """Build the file path for a specific dataset, day, and symbol."""
        name = f"{symbol}.{day}.{dataset}.1min.csv.gz"
        return self.day_folder(dataset, day) / name
    
    def generate_date_range(self) -> List[str]:
        """Generate list of dates in YYYYMMDD format between start_date and end_date (inclusive)."""
        start = datetime.strptime(self.start_date, "%Y%m%d")
        end = datetime.strptime(self.end_date, "%Y%m%d")
        
        dates = []
        current = start
        while current <= end:
            dates.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
        return dates
    
    def resolve_symbols(self, days: List[str]) -> List[str]:
        """Resolve the final list of symbols to use (either provided or auto-discovered)."""
        if self.symbols:
            return self.symbols

        # Use union across datasets and days so we can inspect everything available.
        union = set()
        for day in days:
            for ds in self.datasets:
                union.update(self.discover_symbols(ds, day))
        return sorted(union)
    
    def load_dataset_day(self, dataset: str, day: str, symbols: Iterable[str]) -> pd.DataFrame:
        """Load data for a specific dataset and day across multiple symbols."""
        parts: List[pd.DataFrame] = []
        for symbol in symbols:
            path = self.build_file_path(dataset, day, symbol)
            if not path.exists():
                continue
            df = pd.read_csv(path)
            df["symbol"] = symbol
            df["dataset"] = dataset
            parts.append(df)

        if not parts:
            return pd.DataFrame()

        out = pd.concat(parts, ignore_index=True)
        out["bar_end_utc"] = pd.to_datetime(out["ts_end"], unit="ms", utc=True)
        out = out.sort_values(["symbol", "bar_end_utc"]).reset_index(drop=True)
        return out
    
    def load_dataset_daterange(self, dataset: str, days: List[str], symbols: Iterable[str]) -> pd.DataFrame:
        """Load dataset across multiple days."""
        all_parts: List[pd.DataFrame] = []
        
        for day in days:
            day_df = self.load_dataset_day(dataset, day, symbols)
            if not day_df.empty:
                all_parts.append(day_df)
        
        if not all_parts:
            return pd.DataFrame()
        
        combined = pd.concat(all_parts, ignore_index=True)
        combined = combined.sort_values(["symbol", "bar_end_utc"]).reset_index(drop=True)
        return combined
    
    def downsample_to_interval(self, df: pd.DataFrame) -> pd.DataFrame:
        """Downsample data to the specified interval."""
        if df.empty:
            return df

        # Keep all columns: take first row in each interval bin per symbol.
        parts: List[pd.DataFrame] = []
        for symbol, grp in df.groupby("symbol", sort=True):
            one = (
                grp.set_index("bar_end_utc")
                .resample(f"{self.interval_min}min")
                .first()
                .reset_index()
            )
            one["symbol"] = symbol
            parts.append(one)

        sampled = pd.concat(parts, ignore_index=True)
        sampled = sampled.sort_values(["symbol", "bar_end_utc"]).reset_index(drop=True)
        return sampled
    
    def build_columns_manifest(self, dataset: str, df: pd.DataFrame) -> pd.DataFrame:
        """Build a manifest of columns for a dataset."""
        if df.empty:
            return pd.DataFrame(columns=["dataset", "column", "column_order"])
        return pd.DataFrame(
            {
                "dataset": dataset,
                "column": df.columns.tolist(),
                "column_order": list(range(len(df.columns))),
            }
        )
    
    def safe_write_csv(self, df: pd.DataFrame, path: Path) -> Path:
        """Write CSV with fallback if file is locked."""
        try:
            df.to_csv(path, index=False)
            return path
        except PermissionError:
            fallback = path.with_name(f"{path.stem}_new{path.suffix}")
            df.to_csv(fallback, index=False)
            print(f"[warn] File locked: {path}. Wrote fallback file: {fallback}")
            return fallback
    
    def cleanup_legacy_column_files(self, date_label: str) -> None:
        """Clean up legacy column files."""
        legacy = [
            self.output_dir / f"trade_columns_{date_label}.csv",
            self.output_dir / f"level1_columns_{date_label}.csv",
            self.output_dir / f"book_columns_{date_label}.csv",
            self.output_dir / f"all_datasets_columns_{date_label}.csv",
            self.output_dir / f"columns_trade_{date_label}.csv",
            self.output_dir / f"columns_level1_{date_label}.csv",
            self.output_dir / f"columns_book_{date_label}.csv",
            self.output_dir / f"columns_all_datasets_{date_label}.csv",
        ]
        for path in legacy:
            if path.exists():
                try:
                    path.unlink()
                except PermissionError:
                    print(f"[warn] Could not remove locked legacy file: {path}")
    
    def combine_datasets_for_output(self, sampled_by_dataset: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """Combine multiple datasets into a single dataframe."""
        keys = ["symbol", "bar_end_utc"]
        merged: Optional[pd.DataFrame] = None

        for dataset, df in sampled_by_dataset.items():
            if df.empty:
                continue

            renamed = df.rename(
                columns={c: f"{c}_{dataset}" for c in df.columns if c not in keys}
            )

            if merged is None:
                merged = renamed
            else:
                merged = merged.merge(renamed, on=keys, how="outer")

        if merged is None:
            return pd.DataFrame()

        return merged.sort_values(keys).reset_index(drop=True)
    
    def extract(self, verbose: bool = True) -> Dict[str, any]:
        """
        Extract and process data according to the configured parameters.
        
        Args:
            verbose: Whether to print progress information
        
        Returns:
            Dictionary containing:
                - 'sampled_by_dataset': Dict of DataFrames for each dataset
                - 'combined_data': Combined DataFrame across all datasets
                - 'run_summary': List of summary information for each dataset
                - 'output_files': Dict of output file paths
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate date range
        days = self.generate_date_range()
        date_label = f"{self.start_date}_to_{self.end_date}" if self.start_date != self.end_date else self.start_date
        
        if verbose:
            print(f"Loading data from {self.start_date} to {self.end_date} ({len(days)} day(s))")
        
        symbols = self.resolve_symbols(days)

        if not symbols:
            raise FileNotFoundError(
                f"No symbols found for date range {self.start_date} to {self.end_date} under {self.data_root}"
            )

        if verbose:
            print(f"Using {len(symbols)} symbol(s): {symbols[:10]}{'...' if len(symbols) > 10 else ''}")

        manifests: List[pd.DataFrame] = []
        sampled_by_dataset: Dict[str, pd.DataFrame] = {}
        run_summary = []
        output_files = {}

        for dataset in self.datasets:
            raw = self.load_dataset_daterange(dataset, days, symbols)
            if raw.empty:
                if verbose:
                    print(f"[{dataset}] no files loaded for date range {self.start_date} to {self.end_date}")
                continue

            sampled = self.downsample_to_interval(raw)
            sample_path = self.output_dir / f"{dataset}_{date_label}_{self.interval_min}min_all_columns.csv"
            sample_path = self.safe_write_csv(sampled, sample_path)
            sampled_by_dataset[dataset] = sampled
            output_files[dataset] = str(sample_path)

            manifest = self.build_columns_manifest(dataset, raw)
            manifests.append(manifest)

            run_summary.append(
                {
                    "dataset": dataset,
                    "date_range": f"{self.start_date} to {self.end_date}",
                    "days_count": len(days),
                    "symbols_loaded": sampled["symbol"].nunique(),
                    "raw_rows": len(raw),
                    "sample_rows": len(sampled),
                    "column_count": raw.shape[1],
                    "sample_file": str(sample_path),
                }
            )

            if verbose:
                print(
                    f"[{dataset}] raw_rows={len(raw):,} sample_rows={len(sampled):,} "
                    f"cols={raw.shape[1]} -> {sample_path}"
                )

        combined_data = pd.DataFrame()
        
        if manifests:
            self.cleanup_legacy_column_files(date_label)
            all_columns = pd.concat(manifests, ignore_index=True)
            all_columns = all_columns.sort_values(["dataset", "column_order"]).reset_index(drop=True)

            # 1) Single columns summary file across selected datasets.
            columns_summary_path = self.output_dir / f"columns_summary_{date_label}.csv"
            self.safe_write_csv(all_columns, columns_summary_path)
            output_files['columns_summary'] = str(columns_summary_path)

        if sampled_by_dataset:
            # 2) Combined sampled dataset across all selected datasets.
            combined_data = self.combine_datasets_for_output(sampled_by_dataset)
            if not combined_data.empty:
                combined_data_path = self.output_dir / f"combined_{date_label}_{self.interval_min}min_all_datasets.csv"
                combined_data_path = self.safe_write_csv(combined_data, combined_data_path)
                output_files['combined'] = str(combined_data_path)
                if verbose:
                    print(f"[combined] rows={len(combined_data):,} cols={combined_data.shape[1]} -> {combined_data_path}")

        if run_summary:
            summary_path = self.output_dir / f"run_summary_{date_label}_{self.interval_min}min.csv"
            self.safe_write_csv(
                pd.DataFrame(run_summary),
                summary_path,
            )
            output_files['run_summary'] = str(summary_path)
            if verbose:
                print(f"\nSaved outputs to: {self.output_dir.resolve()}")
        
        return {
            'sampled_by_dataset': sampled_by_dataset,
            'combined_data': combined_data,
            'run_summary': run_summary,
            'output_files': output_files,
        }


# Legacy helper functions (maintained for backward compatibility)
def day_folder(dataset: str, day: str) -> Path:
    return DEFAULT_DATA_ROOT / TYPE_MAP[dataset] / day



# Legacy helper functions (maintained for backward compatibility)
def day_folder(dataset: str, day: str) -> Path:
    return DEFAULT_DATA_ROOT / TYPE_MAP[dataset] / day


def discover_symbols(dataset: str, day: str) -> List[str]:
    extractor = DataExtractor()
    return extractor.discover_symbols(dataset, day)


def build_file_path(dataset: str, day: str, symbol: str) -> Path:
    extractor = DataExtractor()
    return extractor.build_file_path(dataset, day, symbol)


def generate_date_range(start_date: str, end_date: str) -> List[str]:
    """Generate list of dates in YYYYMMDD format between start_date and end_date (inclusive)."""
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


def resolve_symbols(days: List[str], datasets: Iterable[str], selected_symbols: Optional[List[str]]) -> List[str]:
    if selected_symbols:
        return selected_symbols

    # Use union across datasets and days so we can inspect everything available.
    union = set()
    extractor = DataExtractor()
    for day in days:
        for ds in datasets:
            union.update(extractor.discover_symbols(ds, day))
    return sorted(union)


def load_dataset_day(dataset: str, day: str, symbols: Iterable[str]) -> pd.DataFrame:
    extractor = DataExtractor()
    return extractor.load_dataset_day(dataset, day, symbols)


def load_dataset_daterange(dataset: str, days: List[str], symbols: Iterable[str]) -> pd.DataFrame:
    extractor = DataExtractor()
    return extractor.load_dataset_daterange(dataset, days, symbols)


def downsample_to_60m(df: pd.DataFrame, interval_min: int) -> pd.DataFrame:
    if df.empty:
        return df

    # Keep all columns: take first row in each interval bin per symbol.
    parts: List[pd.DataFrame] = []
    for symbol, grp in df.groupby("symbol", sort=True):
        one = (
            grp.set_index("bar_end_utc")
            .resample(f"{interval_min}min")
            .first()
            .reset_index()
        )
        one["symbol"] = symbol
        parts.append(one)

    sampled = pd.concat(parts, ignore_index=True)
    sampled = sampled.sort_values(["symbol", "bar_end_utc"]).reset_index(drop=True)
    return sampled


def build_columns_manifest(dataset: str, df: pd.DataFrame) -> pd.DataFrame:
    extractor = DataExtractor()
    return extractor.build_columns_manifest(dataset, df)



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract data over a time horizon and column manifests for trade/level1/book."
    )
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Start day in YYYYMMDD format.")
    parser.add_argument("--end-date", default=DEFAULT_END_DATE, help="End day in YYYYMMDD format (inclusive).")
    parser.add_argument("--interval-min", default=DEFAULT_INTERVAL_MIN, type=int, help="Downsample interval in minutes.")
    parser.add_argument(
        "--datasets",
        default=",".join(DEFAULT_DATASETS),
        help="Comma-separated datasets to load. Example: trade,level1,book",
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated symbols. Use empty string to load all symbols available for the selected date range.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for CSV outputs.",
    )
    parser.add_argument(
        "--binance-api",
        action="store_true",
        default=True,  # Default to using Binance API
        help="Download Binance klines via API instead of local datasets.",
    )
    parser.add_argument(
        "--binance-market",
        default="spot-us",
        choices=sorted(BINANCE_ENDPOINTS),
        help="Binance market to query (spot, spot-us, usdt-futures, coin-futures).",
    )
    parser.add_argument(
        "--binance-symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated Binance symbols (e.g., BTCUSD, BTCUSDT, BTCUSD_PERP).",
    )
    parser.add_argument(
        "--binance-interval-min",
        default=None,
        type=int,
        help="Override interval for Binance API; defaults to --interval-min.",
    )
    return parser.parse_args()


def parse_csv_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def safe_write_csv(df: pd.DataFrame, path: Path) -> Path:
    try:
        df.to_csv(path, index=False)
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_new{path.suffix}")
        df.to_csv(fallback, index=False)
        print(f"[warn] File locked: {path}. Wrote fallback file: {fallback}")
        return fallback


def cleanup_legacy_column_files(output_dir: Path, date_label: str) -> None:
    legacy = [
        output_dir / f"trade_columns_{date_label}.csv",
        output_dir / f"level1_columns_{date_label}.csv",
        output_dir / f"book_columns_{date_label}.csv",
        output_dir / f"all_datasets_columns_{date_label}.csv",
        output_dir / f"columns_trade_{date_label}.csv",
        output_dir / f"columns_level1_{date_label}.csv",
        output_dir / f"columns_book_{date_label}.csv",
        output_dir / f"columns_all_datasets_{date_label}.csv",
    ]
    for path in legacy:
        if path.exists():
            try:
                path.unlink()
            except PermissionError:
                print(f"[warn] Could not remove locked legacy file: {path}")


def combine_datasets_for_output(sampled_by_dataset: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    keys = ["symbol", "bar_end_utc"]
    merged: Optional[pd.DataFrame] = None

    for dataset, df in sampled_by_dataset.items():
        if df.empty:
            continue

        renamed = df.rename(
            columns={c: f"{c}_{dataset}" for c in df.columns if c not in keys}
        )

        if merged is None:
            merged = renamed
        else:
            merged = merged.merge(renamed, on=keys, how="outer")

    if merged is None:
        return pd.DataFrame()

    return merged.sort_values(keys).reset_index(drop=True)


def main() -> None:
    """Main function for command-line usage."""
    args = parse_args()

    # Default to Binance API mode if no specific mode is set
    if args.binance_api or not args.datasets:
        symbols = parse_csv_list(args.binance_symbols) or DEFAULT_SYMBOLS
        interval_min = args.binance_interval_min or args.interval_min
        market = args.binance_market if hasattr(args, 'binance_market') and args.binance_market else DEFAULT_BINANCE_MARKET
        result = extract_binance_klines(
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            interval_min=interval_min,
            market=market,
            output_dir=Path(args.output_dir),
            verbose=True,
        )
        if result["output_file"] is None:
            raise FileNotFoundError("No Binance data returned for the requested symbols/date range.")
        return
    
    datasets = parse_csv_list(args.datasets)
    if not datasets:
        raise ValueError("No datasets selected. Provide at least one dataset.")
    
    selected_symbols = parse_csv_list(args.symbols)
    selected_symbols = selected_symbols if selected_symbols else None
    
    # Create and run extractor with parsed arguments
    extractor = DataExtractor(
        output_dir=Path(args.output_dir),
        start_date=args.start_date,
        end_date=args.end_date,
        interval_min=args.interval_min,
        datasets=datasets,
        symbols=selected_symbols,
    )
    
    extractor.extract(verbose=True)


if __name__ == "__main__":
    main()
    
    
    
    
    
    
# #!!!! Useage Example   
# # Import the DataExtractor class
# from Data_Extraction_Script_vJL import DataExtractor

# # # Customize extraction parameters
# extractor = DataExtractor(
#     start_date="20240101",
#     end_date="20241231", 
#     interval_min=1,  # 1-hour bars
#     datasets=["trade", "level1"],
#     symbols=["BTCUSDT", "ETHUSDT"]
# )

# # # Extract the data
# results = extractor.extract(verbose=True)

# # # Access data programmatically
# trade_df = results['sampled_by_dataset']['trade']
# combined_df = results['combined_data']

# print(f"\nTrade data shape: {trade_df.shape}")
# print(f"Combined data shape: {combined_df.shape}")
