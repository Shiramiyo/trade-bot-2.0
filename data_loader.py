"""Historical OHLCV data loader (CSV or yfinance) with 5m/1H/1D resampling."""

from pathlib import Path
from typing import Dict, Tuple
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf

from config import TIMEZONE, YFINANCE_TICKERS

NY_TZ = ZoneInfo(TIMEZONE)


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure lowercase OHLCV columns and America/New_York DatetimeIndex."""
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime")
    elif "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    else:
        df.index = pd.to_datetime(df.index)

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(NY_TZ)
    else:
        df.index = df.index.tz_convert(NY_TZ)

    df = df[["open", "high", "low", "close"]].astype(float).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def resample_timeframes(df_5m: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build aligned 5m, 1H, and Daily (17:00 EST close) DataFrames from 5m OHLCV."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    df_1h = df_5m.resample("1h").agg(agg).dropna()
    # 17:00 EST daily close -> shift by -17h on naive local time, resample 1D, shift back
    naive = df_5m.copy()
    naive.index = naive.index.tz_localize(None)
    df_1d = (naive.shift(-17, freq="h").resample("1D").agg(agg).dropna()).shift(17, freq="h")
    df_1d.index = df_1d.index.tz_localize(NY_TZ, ambiguous="infer", nonexistent="shift_forward")
    return df_5m, df_1h, df_1d


def load_from_csv(csv_path: str | Path) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load 5m OHLCV from a local CSV file and resample 1H and 1D."""
    raw = pd.read_csv(csv_path)
    df_5m = normalize_ohlcv(raw)
    return resample_timeframes(df_5m)


def download_yfinance_5m(
    symbol: str,
    period: str = "59d",
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Download up to 59 days of 5m historical data from Yahoo Finance for quick backtesting.
    """
    ticker = YFINANCE_TICKERS.get(symbol, symbol)
    raw = yf.download(ticker, period=period, interval="5m", progress=False, auto_adjust=False)
    if raw.empty:
        raise ValueError(f"No 5m data returned from yfinance for {symbol} ({ticker})")
    df_5m = normalize_ohlcv(raw)
    return resample_timeframes(df_5m)


def load_universe_yfinance(
    symbols: list[str],
    period: str = "59d",
) -> Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]]:
    """Download 5m/1H/1D datasets for all requested symbols."""
    data: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    for sym in symbols:
        try:
            data[sym] = download_yfinance_5m(sym, period=period)
        except Exception as exc:
            print(f"[WARN] Skipping {sym}: {exc}")
    return data
