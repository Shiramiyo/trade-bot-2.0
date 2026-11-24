"""Multi-source market data & watchlist feed for the built-in TradingView charting terminal."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import MetaTrader5 as mt5
import pandas as pd
import yfinance as yf

from live_mt5_bot import _rates_to_ny_df

# Shared lock protecting all MetaTrader 5 IPC calls across HTTP handler threads
MT5_LOCK = threading.RLock()


@dataclass(frozen=True)
class WatchlistSymbol:
    """Canonical watchlist definition mapping TV display symbols to MT5, Binance, and Yahoo feeds."""

    symbol: str
    display_name: str
    category: str
    badge: str
    badge_bg: str
    badge_fg: str
    digits: int
    mt5_candidates: Tuple[str, ...] = ()
    binance_perp: Optional[str] = None
    yf_ticker: Optional[str] = None
    invert: bool = False


# Exact watchlist matching the user's TradingView list
DEFAULT_WATCHLIST: Tuple[WatchlistSymbol, ...] = (
    WatchlistSymbol("BTCUSDT.P", "Bitcoin Perpetual", "CRYPTO", "B", "#f7931a", "#ffffff", 1, ("BTCUSD", "BTCUSDT"), "BTCUSDT", "BTC-USD"),
    WatchlistSymbol("SOLUSDT.P", "Solana Perpetual", "CRYPTO", "S", "#111827", "#14f195", 2, ("SOLUSD", "SOLUSDT"), "SOLUSDT", "SOL-USD"),
    WatchlistSymbol("GOLD", "Spot Gold / US Dollar", "METALS", "Au", "#eab308", "#111827", 3, ("XAUUSD", "XAUUSD.m"), None, "GC=F"),
    WatchlistSymbol("EURUSD", "Euro / U.S. Dollar", "FOREX", "EU", "#1d4ed8", "#ffffff", 5, ("EURUSD", "EURUSD.m"), None, "EURUSD=X"),
    WatchlistSymbol("JPYUSD", "Japanese Yen / U.S. Dollar", "FOREX", "JP", "#dc2626", "#ffffff", 7, ("USDJPY", "USDJPY.m"), None, "6J=F", invert=True),
    WatchlistSymbol("AUDUSD", "Australian Dollar / U.S. Dollar", "FOREX", "AU", "#1e40af", "#ffffff", 5, ("AUDUSD", "AUDUSD.m"), None, "AUDUSD=X"),
    WatchlistSymbol("USDCHF", "U.S. Dollar / Swiss Franc", "FOREX", "CH", "#ef4444", "#ffffff", 5, ("USDCHF", "USDCHF.m"), None, "CHF=X"),
    WatchlistSymbol("USDCAD", "U.S. Dollar / Canadian Dollar", "FOREX", "CA", "#b91c1c", "#ffffff", 5, ("USDCAD", "USDCAD.m"), None, "CAD=X"),
    WatchlistSymbol("GBPUSD", "British Pound / U.S. Dollar", "FOREX", "GB", "#1d4ed8", "#ffffff", 5, ("GBPUSD", "GBPUSD.m"), None, "GBPUSD=X"),
    WatchlistSymbol("USOIL", "WTI Crude Oil", "ENERGY", "OIL", "#111827", "#f8fafc", 2, (), None, "CL=F"),
    WatchlistSymbol("NZDUSD", "New Zealand Dollar / U.S. Dollar", "FOREX", "NZ", "#1e3a8a", "#ffffff", 5, ("NZDUSD", "NZDUSD.m"), None, "NZDUSD=X"),
    WatchlistSymbol("DXY", "U.S. Dollar Index", "INDEX", "$", "#059669", "#ffffff", 3, (), None, "DX-Y.NYB"),
    WatchlistSymbol("NAS100", "Nasdaq 100 Index", "INDEX", "100", "#0284c7", "#ffffff", 2, ("USTEC", "NAS100", "US100"), None, "NQ=F"),
    WatchlistSymbol("ETHUSDT.P", "Ethereum Perpetual", "CRYPTO", "E", "#6366f1", "#ffffff", 2, ("ETHUSD", "ETHUSDT"), "ETHUSDT", "ETH-USD"),
    WatchlistSymbol("HYPEUSDT.P", "Hyperliquid Perpetual", "CRYPTO", "H", "#0f172a", "#2dd4bf", 3, (), "HYPEUSDT", None),
)

WATCHLIST_BY_SYMBOL: Dict[str, WatchlistSymbol] = {item.symbol: item for item in DEFAULT_WATCHLIST}

# Aliases from bot/MT5 canonical names to TradingView watchlist symbols
SYMBOL_ALIASES: Dict[str, str] = {
    "USTEC": "NAS100",
    "NQ": "NAS100",
    "US100": "NAS100",
    "XAUUSD": "GOLD",
    "CL": "USOIL",
    "BTCUSD": "BTCUSDT.P",
    "ETHUSD": "ETHUSDT.P",
    "SOLUSD": "SOLUSDT.P",
}

MT5_TIMEFRAMES: Dict[str, int] = {
    "1m": getattr(mt5, "TIMEFRAME_M1", 1),
    "5m": getattr(mt5, "TIMEFRAME_M5", 5),
    "15m": getattr(mt5, "TIMEFRAME_M15", 15),
    "30m": getattr(mt5, "TIMEFRAME_M30", 30),
    "1h": getattr(mt5, "TIMEFRAME_H1", 16385),
    "4h": getattr(mt5, "TIMEFRAME_H4", 16388),
    "1d": getattr(mt5, "TIMEFRAME_D1", 16408),
    "1w": getattr(mt5, "TIMEFRAME_W1", 32769),
}

BINANCE_INTERVALS: Dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
}

YF_INTERVAL_CONFIG: Dict[str, Tuple[str, str, Optional[str]]] = {
    # tf -> (yf_interval, yf_period, optional_resample_rule)
    "1m": ("1m", "5d", None),
    "5m": ("5m", "30d", None),
    "15m": ("15m", "60d", None),
    "30m": ("30m", "60d", None),
    "1h": ("1h", "730d", None),
    "4h": ("1h", "730d", "4h"),
    "1d": ("1d", "5y", None),
    "1w": ("1wk", "10y", None),
}


def _http_get_json(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "TradeTerminal/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _infer_digits(price: float, default: int = 2) -> int:
    if price <= 0:
        return default
    if price < 0.05:
        return 7
    if price < 2.0:
        return 5
    if price < 20.0:
        return 4
    if price < 200.0:
        return 3
    return default


def _invert_ohlc_row(o: float, h: float, l: float, c: float) -> Tuple[float, float, float, float]:
    """Invert OHLC prices (e.g. USDJPY -> JPYUSD) while keeping high >= low."""
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        return o, h, l, c
    return 1.0 / o, 1.0 / l, 1.0 / h, 1.0 / c


def _extract_volume(row: Any) -> float:
    for col in ("tick_volume", "real_volume", "Volume", "volume"):
        if col in row:
            val = row[col]
            if pd.notna(val) and float(val) > 0:
                return float(val)
    return 0.0


def _df_to_candle_list(df: pd.DataFrame, digits: int, invert: bool = False) -> List[Dict[str, Any]]:
    candles: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return candles
    for idx, row in df.iterrows():
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        if any(pd.isna(v) for v in (o, h, l, c)):
            continue
        if invert:
            o, h, l, c = _invert_ohlc_row(o, h, l, c)
        vol = _extract_volume(row)
        candles.append(
            {
                "time": int(pd.Timestamp(idx).tz_convert(timezone.utc).timestamp()),
                "open": round(o, digits),
                "high": round(h, digits),
                "low": round(l, digits),
                "close": round(c, digits),
                "volume": round(vol, 4),
            }
        )
    return candles


def resolve_watchlist_spec(symbol: str) -> WatchlistSymbol:
    """Resolve a requested symbol (including aliases or custom tickers) into a `WatchlistSymbol`."""
    raw = (symbol or "NAS100").strip().upper()
    canonical = SYMBOL_ALIASES.get(raw, raw)
    if canonical in WATCHLIST_BY_SYMBOL:
        return WATCHLIST_BY_SYMBOL[canonical]

    # Dynamic custom symbol fallback (supports Binance *.P / *USDT or Yahoo/MT5 tickers)
    if raw.endswith(".P") or raw.endswith("USDT"):
        perp = raw[:-2] if raw.endswith(".P") else raw
        badge = perp.replace("USDT", "")[:3] or "CR"
        return WatchlistSymbol(raw, f"{perp} Perpetual", "CUSTOM", badge, "#334155", "#f8fafc", 2, (perp,), perp, None)

    return WatchlistSymbol(raw, raw, "CUSTOM", raw[:3], "#334155", "#f8fafc", 2, (raw,), None, raw)


class MarketFeedService:
    """Thread-safe, TTL-cached multi-source quote & OHLCV provider for MT5, Binance Futures, and Yahoo."""

    def __init__(
        self,
        server_offset_hours: int = 7,
        quote_ttl_seconds: float = 4.0,
        chart_ttl_seconds: float = 8.0,
        http_getter: Callable[[str], Any] = _http_get_json,
    ) -> None:
        self.server_offset_hours = server_offset_hours
        self.quote_ttl_seconds = quote_ttl_seconds
        self.chart_ttl_seconds = chart_ttl_seconds
        self._http_get = http_getter
        self._lock = threading.Lock()
        self._mt5_ready = False
        self._mt5_symbol_set: Optional[set[str]] = None
        self._quote_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        self._chart_cache: Dict[Tuple[str, str, int], Tuple[float, Dict[str, Any]]] = {}

    def _ensure_mt5(self) -> bool:
        with MT5_LOCK:
            try:
                if self._mt5_ready and mt5.account_info() is not None:
                    return True
                self._mt5_ready = bool(mt5.initialize())
                if not self._mt5_ready:
                    self._mt5_symbol_set = None
                return self._mt5_ready
            except Exception:
                return False

    def _find_mt5_symbol(self, candidates: Tuple[str, ...]) -> Optional[str]:
        if not candidates:
            return None
        with MT5_LOCK:
            if not self._ensure_mt5():
                return None
            for cand in candidates:
                try:
                    if mt5.symbol_info(cand) is not None:
                        mt5.symbol_select(cand, True)
                        return cand
                except Exception:
                    continue
            if self._mt5_symbol_set is None:
                try:
                    self._mt5_symbol_set = {s.name for s in (mt5.symbols_get() or [])}
                except Exception:
                    self._mt5_symbol_set = set()
            for cand in candidates:
                if cand in self._mt5_symbol_set:
                    try:
                        mt5.symbol_select(cand, True)
                    except Exception:
                        pass
                    return cand
            return None

    def _fetch_binance_quotes(self) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        try:
            rows = self._http_get("https://fapi.binance.com/fapi/v1/ticker/24hr")
            wanted = {s.binance_perp for s in DEFAULT_WATCHLIST if s.binance_perp}
            for item in rows or []:
                sym = item.get("symbol")
                if sym in wanted:
                    last = float(item.get("lastPrice", 0.0) or 0.0)
                    chg = float(item.get("priceChange", 0.0) or 0.0)
                    pct = float(item.get("priceChangePercent", 0.0) or 0.0)
                    out[sym] = {
                        "price": last,
                        "prev_close": last - chg,
                        "change": chg,
                        "change_pct": pct,
                        "volume": float(item.get("quoteVolume", 0.0) or 0.0),
                    }
        except Exception:
            pass
        return out

    def _fetch_yf_quotes(self, tickers: List[str]) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        if not tickers:
            return out
        try:
            df = yf.download(tickers, period="5d", interval="1d", progress=False, auto_adjust=False, threads=True)
            if df is None or df.empty:
                return out
            closes = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": tickers[0]})
            for tk in tickers:
                if tk not in closes.columns:
                    continue
                ser = closes[tk].dropna()
                if ser.empty:
                    continue
                last = float(ser.iloc[-1])
                prev = float(ser.iloc[-2]) if len(ser) >= 2 else last
                chg = last - prev
                pct = (chg / prev * 100.0) if prev > 0 else 0.0
                out[tk] = {"price": last, "prev_close": prev, "change": chg, "change_pct": pct}
        except Exception:
            pass
        return out

    def _fetch_mt5_quote(self, spec: WatchlistSymbol) -> Optional[Dict[str, float]]:
        with MT5_LOCK:
            mt5_sym = self._find_mt5_symbol(spec.mt5_candidates)
            if not mt5_sym:
                return None
            try:
                tick = mt5.symbol_info_tick(mt5_sym)
                d1 = mt5.copy_rates_from_pos(mt5_sym, MT5_TIMEFRAMES["1d"], 0, 3)
            except Exception:
                return None

        bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick is not None else 0.0
        ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick is not None else 0.0
        raw_last = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (bid or ask)
        if raw_last <= 0 and d1 is not None and len(d1) > 0:
            raw_last = float(d1[-1]["close"])
        if raw_last <= 0:
            return None

        raw_prev = raw_last
        if d1 is not None and len(d1) >= 2:
            raw_prev = float(d1[-2]["close"])

        if spec.invert:
            last = 1.0 / raw_last
            prev = 1.0 / raw_prev if raw_prev > 0 else last
            bid_out = 1.0 / ask if ask > 0 else last
            ask_out = 1.0 / bid if bid > 0 else last
        else:
            last = raw_last
            prev = raw_prev
            bid_out = bid or last
            ask_out = ask or last

        chg = last - prev
        pct = (chg / prev * 100.0) if prev > 0 else 0.0
        return {
            "price": last,
            "prev_close": prev,
            "change": chg,
            "change_pct": pct,
            "bid": bid_out,
            "ask": ask_out,
            "source_symbol": mt5_sym,  # type: ignore[dict-item]
        }

    def get_watchlist(self, extra_symbols: Optional[List[str]] = None, force: bool = False) -> Dict[str, Any]:
        """Return live quotes for all 15 TradingView watchlist symbols plus any custom symbols."""
        now_mono = time.monotonic()
        extra_specs = [resolve_watchlist_spec(s) for s in (extra_symbols or []) if s.strip().upper() not in WATCHLIST_BY_SYMBOL]
        use_cache = not force and not extra_specs

        if use_cache and self._quote_cache is not None:
            ts, cached_rows = self._quote_cache
            if (now_mono - ts) < self.quote_ttl_seconds:
                return {"symbols": cached_rows, "updated_at_epoch": int(time.time())}

        specs = list(DEFAULT_WATCHLIST) + extra_specs
        mt5_quotes: Dict[str, Dict[str, float]] = {}
        yf_needed: List[str] = []

        # Run Binance batch quote fetch in parallel with MT5 lookups
        with ThreadPoolExecutor(max_workers=2) as pool:
            binance_future = pool.submit(self._fetch_binance_quotes)
            for spec in specs:
                if spec.binance_perp:
                    continue
                q_mt5 = self._fetch_mt5_quote(spec)
                if q_mt5 is not None:
                    mt5_quotes[spec.symbol] = q_mt5
                elif spec.yf_ticker:
                    yf_needed.append(spec.yf_ticker)
            binance_quotes = binance_future.result()

        # Fallback to Yahoo for any Binance symbol that failed offline
        for spec in specs:
            if spec.binance_perp and spec.binance_perp not in binance_quotes:
                q_mt5 = self._fetch_mt5_quote(spec)
                if q_mt5 is not None:
                    mt5_quotes[spec.symbol] = q_mt5
                elif spec.yf_ticker:
                    yf_needed.append(spec.yf_ticker)

        yf_quotes = self._fetch_yf_quotes(list(dict.fromkeys(yf_needed)))

        rows: List[Dict[str, Any]] = []
        for spec in specs:
            q: Optional[Dict[str, Any]] = None
            source = "offline"
            if spec.binance_perp and spec.binance_perp in binance_quotes:
                q = binance_quotes[spec.binance_perp]
                source = f"Binance {spec.binance_perp}"
            elif spec.symbol in mt5_quotes:
                q = mt5_quotes[spec.symbol]
                src_sym = q.get("source_symbol", spec.symbol)
                source = f"MT5 {src_sym}" + (" (1/x)" if spec.invert else "")
            elif spec.yf_ticker and spec.yf_ticker in yf_quotes:
                q = yf_quotes[spec.yf_ticker]
                source = f"Yahoo {spec.yf_ticker}"

            price = float(q["price"]) if q else 0.0
            digits = spec.digits if spec.category != "CUSTOM" else _infer_digits(price, spec.digits)
            chg = float(q["change"]) if q else 0.0
            pct = float(q["change_pct"]) if q else 0.0

            rows.append(
                {
                    "symbol": spec.symbol,
                    "display_name": spec.display_name,
                    "category": spec.category,
                    "badge": spec.badge,
                    "badge_bg": spec.badge_bg,
                    "badge_fg": spec.badge_fg,
                    "digits": digits,
                    "price": round(price, digits) if q else None,
                    "change": round(chg, digits) if q else None,
                    "change_pct": round(pct, 2) if q else None,
                    "bid": round(float(q.get("bid", price)), digits) if q else None,
                    "ask": round(float(q.get("ask", price)), digits) if q else None,
                    "source": source,
                }
            )

        if not extra_specs:
            self._quote_cache = (now_mono, rows)
        return {"symbols": rows, "updated_at_epoch": int(time.time())}

    def _fetch_binance_candles(self, perp_symbol: str, tf: str, bars: int, digits: int) -> Tuple[List[Dict[str, Any]], str]:
        iv = BINANCE_INTERVALS.get(tf, "1h")
        limit = max(60, min(1000, bars))
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={perp_symbol}&interval={iv}&limit={limit}"
        try:
            raw = self._http_get(url)
            candles = [
                {
                    "time": int(k[0] // 1000),
                    "open": round(float(k[1]), digits),
                    "high": round(float(k[2]), digits),
                    "low": round(float(k[3]), digits),
                    "close": round(float(k[4]), digits),
                    "volume": round(float(k[5]), 4),
                }
                for k in (raw or [])
            ]
            if candles:
                return candles, f"Binance Perp ({perp_symbol})"
        except Exception:
            pass
        return [], ""

    def _fetch_mt5_candles(
        self,
        spec: WatchlistSymbol,
        tf: str,
        bars: int,
    ) -> Tuple[List[Dict[str, Any]], str, int, Optional[float], Optional[float]]:
        tf_code = MT5_TIMEFRAMES.get(tf, MT5_TIMEFRAMES["1h"])
        with MT5_LOCK:
            mt5_sym = self._find_mt5_symbol(spec.mt5_candidates)
            if not mt5_sym:
                return [], "", spec.digits, None, None
            try:
                rates = mt5.copy_rates_from_pos(mt5_sym, tf_code, 0, max(120, min(1200, bars)))
                tick = mt5.symbol_info_tick(mt5_sym)
                info = mt5.symbol_info(mt5_sym)
            except Exception:
                return [], "", spec.digits, None, None
        if rates is None or len(rates) == 0:
            return [], "", spec.digits, None, None

        df = pd.DataFrame(rates)
        ny_df = _rates_to_ny_df(rates, server_offset_hours=self.server_offset_hours)
        if "tick_volume" in df.columns or "real_volume" in df.columns:
            vol_col = "real_volume" if ("real_volume" in df.columns and float(df["real_volume"].sum()) > 0) else "tick_volume"
            ny_naive = pd.to_datetime(df["time"].astype("int64"), unit="s") - pd.Timedelta(hours=self.server_offset_hours)
            ny_aware = ny_naive.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward")
            vol_df = pd.DataFrame({"timestamp": ny_aware, "volume": df[vol_col].astype(float)}).dropna(subset=["timestamp"])
            vol_df = vol_df.drop_duplicates(subset=["timestamp"], keep="last").set_index("timestamp")
            ny_df["volume"] = vol_df["volume"].reindex(ny_df.index).fillna(0.0)
        else:
            ny_df["volume"] = 0.0

        digits = spec.digits
        if spec.category == "CUSTOM" and info is not None:
            digits = int(getattr(info, "digits", spec.digits) or spec.digits)
        candles = _df_to_candle_list(ny_df.tail(bars), digits=digits, invert=spec.invert)

        bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick is not None else 0.0
        ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick is not None else 0.0
        if spec.invert and bid > 0 and ask > 0:
            bid, ask = 1.0 / ask, 1.0 / bid
        src = f"MT5 {mt5_sym}" + (" (1/x)" if spec.invert else "")
        return candles, src, digits, (round(bid, digits) if bid > 0 else None), (round(ask, digits) if ask > 0 else None)

    def _fetch_yf_candles(self, spec: WatchlistSymbol, tf: str, bars: int) -> Tuple[List[Dict[str, Any]], str, int]:
        if not spec.yf_ticker:
            return [], "", spec.digits
        iv, period, resample_rule = YF_INTERVAL_CONFIG.get(tf, YF_INTERVAL_CONFIG["1h"])
        try:
            df = yf.Ticker(spec.yf_ticker).history(period=period, interval=iv, auto_adjust=False)
        except Exception:
            return [], "", spec.digits
        if df is None or df.empty:
            return [], "", spec.digits

        renamed = pd.DataFrame(
            {
                "open": df["Open"].astype(float),
                "high": df["High"].astype(float),
                "low": df["Low"].astype(float),
                "close": df["Close"].astype(float),
                "volume": df["Volume"].astype(float) if "Volume" in df.columns else 0.0,
            },
            index=pd.DatetimeIndex(df.index).tz_convert(timezone.utc) if df.index.tzinfo else pd.DatetimeIndex(df.index).tz_localize(timezone.utc),
        ).dropna(subset=["open", "high", "low", "close"])

        if resample_rule:
            renamed = (
                renamed.resample(resample_rule)
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                .dropna(subset=["open", "high", "low", "close"])
            )

        # Yahoo `6J=F` is already quoted in JPYUSD (~0.00629), so never double-invert it.
        invert_yf = False
        digits = spec.digits
        if spec.category == "CUSTOM" and not renamed.empty:
            digits = _infer_digits(float(renamed["close"].iloc[-1]), spec.digits)
        candles = _df_to_candle_list(renamed.tail(bars), digits=digits, invert=invert_yf)
        return candles, f"Yahoo {spec.yf_ticker}", digits

    def get_chart(self, symbol: str = "NAS100", timeframe: str = "1h", bars: int = 500) -> Dict[str, Any]:
        """Fetch OHLCV candles and quote metadata for any watchlist or custom symbol."""
        spec = resolve_watchlist_spec(symbol)
        tf = timeframe.lower() if timeframe.lower() in MT5_TIMEFRAMES else "1h"
        bars = max(60, min(1000, int(bars)))
        cache_key = (spec.symbol, tf, bars)
        now_mono = time.monotonic()

        cached = self._chart_cache.get(cache_key)
        if cached and (now_mono - cached[0]) < self.chart_ttl_seconds:
            return cached[1]

        candles: List[Dict[str, Any]] = []
        source = "offline"
        digits = spec.digits
        bid: Optional[float] = None
        ask: Optional[float] = None

        if spec.binance_perp:
            candles, source = self._fetch_binance_candles(spec.binance_perp, tf, bars, digits)

        if not candles and spec.mt5_candidates:
            candles, source, digits, bid, ask = self._fetch_mt5_candles(spec, tf, bars)

        if not candles and spec.yf_ticker:
            candles, source, digits = self._fetch_yf_candles(spec, tf, bars)

        last_close = candles[-1]["close"] if candles else None
        prev_close = candles[-2]["close"] if len(candles) >= 2 else last_close
        change = round(last_close - prev_close, digits) if (last_close is not None and prev_close is not None) else None
        change_pct = round((change / prev_close) * 100.0, 2) if (change is not None and prev_close) else None

        payload: Dict[str, Any] = {
            "symbol": spec.symbol,
            "display_name": spec.display_name,
            "category": spec.category,
            "timeframe": tf,
            "digits": digits,
            "source": source or "Unavailable",
            "price": last_close,
            "bid": bid if bid is not None else last_close,
            "ask": ask if ask is not None else last_close,
            "change": change,
            "change_pct": change_pct,
            "candles": candles,
            "error": None if candles else f"No market data available for {spec.symbol} ({tf})",
        }
        if candles:
            self._chart_cache[cache_key] = (now_mono, payload)
        return payload
