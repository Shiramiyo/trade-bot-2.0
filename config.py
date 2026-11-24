"""Strategy configuration: universe, sessions, risk rules, and news settings."""

from dataclasses import dataclass, field
from datetime import time
from typing import Dict, List


TIMEZONE = "America/New_York"

# Exact tradable universe
FOREX_MAJORS: List[str] = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "NZDUSD",
]
CRYPTO_SYMBOLS: List[str] = ["BTCUSD"]
METAL_SYMBOLS: List[str] = ["XAUUSD"]
FUTURES_SYMBOLS: List[str] = ["CL", "NQ"]

ALL_SYMBOLS: List[str] = FOREX_MAJORS + CRYPTO_SYMBOLS + METAL_SYMBOLS + FUTURES_SYMBOLS

# Yahoo Finance ticker mapping for quick sample backtests
YFINANCE_TICKERS: Dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",
    "NZDUSD": "NZDUSD=X",
    "BTCUSD": "BTC-USD",
    "XAUUSD": "GC=F",
    "CL": "CL=F",
    "NQ": "NQ=F",
}

# Currency -> affected symbols mapping for news filtering
CURRENCY_TO_SYMBOLS: Dict[str, List[str]] = {
    "USD": ALL_SYMBOLS,  # USD news affects all USD-quoted majors, Gold, BTC, CL, NQ
    "EUR": ["EURUSD"],
    "GBP": ["GBPUSD"],
    "JPY": ["USDJPY"],
    "AUD": ["AUDUSD"],
    "CAD": ["USDCAD"],
    "CHF": ["USDCHF"],
    "NZD": ["NZDUSD"],
}


@dataclass(frozen=True)
class SessionConfig:
    """Trading session definition in New York (America/New_York) local time."""

    name: str
    open_time: time
    symbols: List[str]
    max_trades: int
    pre_open_minutes: int = 60
    post_open_minutes: int = 60


# Session windows: 1 hour before to 1 hour after session open
SESSIONS: Dict[str, SessionConfig] = {
    "ASIA": SessionConfig(
        name="ASIA",
        open_time=time(19, 0),  # 19:00 EST -> window 18:00 - 20:00 EST
        symbols=["AUDUSD", "USDJPY"],
        max_trades=1,
    ),
    "LONDON": SessionConfig(
        name="LONDON",
        open_time=time(3, 0),   # 03:00 EST -> window 02:00 - 04:00 EST
        symbols=["GBPUSD", "EURUSD"],
        max_trades=2,
    ),
    "NEW_YORK": SessionConfig(
        name="NEW_YORK",
        open_time=time(8, 30),  # 08:30 EST -> window 07:30 - 09:30 EST
        symbols=ALL_SYMBOLS,
        max_trades=2,
    ),
}

# Map non-USD currencies to their primary local session
CURRENCY_TO_SESSION: Dict[str, str] = {
    "AUD": "ASIA",
    "NZD": "ASIA",
    "JPY": "ASIA",
    "GBP": "LONDON",
    "EUR": "LONDON",
    "CHF": "LONDON",
    "CAD": "NEW_YORK",
}


@dataclass(frozen=True)
class SymbolCost:
    """Realistic round-turn spread, slippage (in price units), and commission rate (fraction of notional)."""

    spread: float
    slippage_per_side: float
    commission_rate: float = 0.000035  # ~ $3.50 per $100k notional per side ($7/lot round-turn)


SYMBOL_COSTS: Dict[str, SymbolCost] = {
    "EURUSD": SymbolCost(spread=0.00008, slippage_per_side=0.00002),
    "GBPUSD": SymbolCost(spread=0.00010, slippage_per_side=0.00002),
    "USDJPY": SymbolCost(spread=0.009, slippage_per_side=0.002),
    "AUDUSD": SymbolCost(spread=0.00010, slippage_per_side=0.00002),
    "USDCAD": SymbolCost(spread=0.00012, slippage_per_side=0.00002),
    "USDCHF": SymbolCost(spread=0.00012, slippage_per_side=0.00002),
    "NZDUSD": SymbolCost(spread=0.00012, slippage_per_side=0.00002),
    "BTCUSD": SymbolCost(spread=15.0, slippage_per_side=5.0, commission_rate=0.0004),
    "XAUUSD": SymbolCost(spread=0.25, slippage_per_side=0.10, commission_rate=0.000035),
    "CL": SymbolCost(spread=0.03, slippage_per_side=0.01, commission_rate=0.00005),
    "NQ": SymbolCost(spread=1.0, slippage_per_side=0.5, commission_rate=0.00002),
}


@dataclass
class StrategyConfig:
    """Tunable parameters for backtesting and live execution."""

    initial_capital: float = 100_000.0
    base_risk_pct: float = 0.01          # 1.0% normal risk
    ranging_risk_pct: float = 0.005      # 0.5% ranging day risk
    monday_no_news_risk_pct: float = 0.005  # 0.5% Monday no-news risk
    default_rr: float = 2.0              # Minimum / default 2R target
    be_min_rr: float = 1.0               # Move to BE when price is 1.0 - 1.5 RR
    be_max_rr: float = 1.5
    be_same_dir_cisds: int = 2           # 2 more CISDs in trade direction
    cut_opp_consec_cisds: int = 2        # 2 consecutive opposite CISDs to cut/BE
    aoi_lookback_days: int = 1           # Primary lookback = 1 day, fallback = 2 days
    aoi_max_lookback_days: int = 2
    ranging_lookback_days: int = 3       # Unbroken high/low over last 2-3 days
    doji_body_ratio: float = 0.05        # Body <= 5% of candle range counts as Doji
    eq_tolerance_atr: float = 0.15       # Relative equal high/low tolerance vs ATR
    swing_lookback: int = 3              # Fractal swing lookback (bars left/right)
    daily_close_hour_est: int = 17       # 17:00 EST New York daily candle close
    news_window_minutes: int = 60        # +/- 1 hour around high-impact news
    limit_expiry_minutes: int = 120      # Cancel unfilled limit after 2 hours
    year_end_blackout_days: int = 5      # Do not trade the last 5 days of the year (Dec 27-31)
    apply_costs: bool = True             # Deduct spread, slippage, and commissions
    max_cost_to_risk_ratio: float = 0.20 # Skip trades where costs exceed 20% of 1R risk
    cisd_min_body_ratio: float = 0.50    # Undeniable CISD: body >= 50% of its range
    require_5m_sweep_for_cisd: bool = True
    news_only: bool = False              # Only trade on high-impact news days
    let_play_out: bool = False           # Disable BE & early CISD cut; let trade hit SL/TP
    ny_only: bool = True                 # Trade ONLY New York session
    skip_monday: bool = True             # Do not trade Monday at all (Tue-Fri only)
    force_close_news_1h: bool = False    # Do not force close at +1h after USD news
    symbols: List[str] = field(default_factory=lambda: list(ALL_SYMBOLS))
