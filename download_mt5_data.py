"""Download historical 5-minute OHLCV data from a running MetaTrader 5 terminal."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore[assignment]
import numpy as np
import pandas as pd

from config import ALL_SYMBOLS

# Common broker symbol aliases for each strategy symbol
SYMBOL_ALIASES: Dict[str, List[str]] = {
    "EURUSD": ["EURUSD", "EURUSD.m", "EURUSDm", "EURUSD.pro"],
    "GBPUSD": ["GBPUSD", "GBPUSD.m", "GBPUSDm", "GBPUSD.pro"],
    "USDJPY": ["USDJPY", "USDJPY.m", "USDJPYm", "USDJPY.pro"],
    "AUDUSD": ["AUDUSD", "AUDUSD.m", "AUDUSDm", "AUDUSD.pro"],
    "USDCAD": ["USDCAD", "USDCAD.m", "USDCADm", "USDCAD.pro"],
    "USDCHF": ["USDCHF", "USDCHF.m", "USDCHFm", "USDCHF.pro"],
    "NZDUSD": ["NZDUSD", "NZDUSD.m", "NZDUSDm", "NZDUSD.pro"],
    "BTCUSD": ["BTCUSD", "BTCUSD.m", "BTCUSDT", "BITCOIN"],
    "XAUUSD": ["XAUUSD", "XAUUSD.m", "GOLD", "XAUUSDm"],
    "CL": ["USOIL", "USOUSD", "XTIUSD", "OIL.WTI", "USOIL.m", "CL.f"],
    "NQ": ["NAS100", "US100", "USTEC", "USTECH100M", "NQ100", "NQ", "NDX"],
}


def resolve_mt5_symbol(strategy_sym: str) -> Optional[str]:
    """Find the exact matching symbol name available in the connected MT5 terminal."""
    if mt5 is None:
        return None
    candidates = SYMBOL_ALIASES.get(strategy_sym, [strategy_sym])
    all_mt5 = {s.name for s in (mt5.symbols_get() or [])}

    for cand in candidates:
        if cand in all_mt5:
            mt5.symbol_select(cand, True)
            return cand
    return None


def fetch_all_5m_bars(mt5_sym: str, chunk_size: int = 50_000, max_bars: int = 250_000) -> pd.DataFrame:
    """Page backwards with copy_rates_from_pos to pull all available 5m history."""
    chunks = []
    offset = 0
    while offset < max_bars:
        rates = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_M5, offset, chunk_size)
        if rates is None or len(rates) == 0:
            break
        chunks.append(rates)
        if len(rates) < chunk_size:
            break
        offset += chunk_size

    if not chunks:
        return pd.DataFrame()

    combined = np.concatenate(list(reversed(chunks)))
    df = pd.DataFrame(combined)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df[["timestamp", "open", "high", "low", "close"]].drop_duplicates(subset=["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download 5m data from MT5 into CSVs")
    parser.add_argument("--start", default="2024-12-25", help="Start date YYYY-MM-DD (includes warmup)")
    parser.add_argument("--end", default="2025-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--symbols", nargs="+", default=ALL_SYMBOLS)
    args = parser.parse_args()

    if not mt5.initialize():
        raise SystemExit(
            f"MT5 initialize() failed: {mt5.last_error()}\n"
            "Make sure MetaTrader 5 is open and logged into a demo account."
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    dt_from = pd.Timestamp(args.start, tz="UTC")
    dt_to = pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1)

    try:
        for sym in args.symbols:
            mt5_sym = resolve_mt5_symbol(sym)
            if not mt5_sym:
                print(f"[WARN] Could not find MT5 symbol for {sym}")
                continue

            df = fetch_all_5m_bars(mt5_sym)
            if df.empty:
                print(f"[WARN] No 5m bars returned for {sym} ({mt5_sym}): {mt5.last_error()}")
                continue

            df_filtered = df[(df["timestamp"] >= dt_from) & (df["timestamp"] < dt_to)]
            if df_filtered.empty:
                min_d = df["timestamp"].min().date()
                max_d = df["timestamp"].max().date()
                print(f"[WARN] {sym} ({mt5_sym}) has data from {min_d} to {max_d}, none in {args.start}..{args.end}")
                continue

            out_file = args.out_dir / f"{sym}.csv"
            df_filtered.to_csv(out_file, index=False)
            print(
                f"[OK] {sym} ({mt5_sym}): {len(df_filtered)} bars "
                f"({df_filtered['timestamp'].min().date()} to {df_filtered['timestamp'].max().date()}) -> {out_file}"
            )
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
