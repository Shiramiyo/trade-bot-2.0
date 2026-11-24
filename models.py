"""Core domain models for candles, AOIs, CISD signals, orders, trades, and news."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Bias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NONE = "NONE"


class AOIType(str, Enum):
    FVG = "FVG"
    OB = "OB"


class TradeStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


@dataclass
class AOI:
    """1H (or 5m momentum) Area of Interest: Fair Value Gap or Order Block."""

    aoi_type: AOIType
    direction: Bias          # BULLISH (support/demand) or BEARISH (resistance/supply)
    top: float
    bottom: float
    body_top: float
    body_bottom: float
    created_at: datetime
    timeframe: str = "1H"
    mitigated: bool = False

    def contains_price(self, low: float, high: float) -> bool:
        """Return True if a candle [low, high] overlaps this AOI zone."""
        return high >= self.bottom and low <= self.top


@dataclass
class LiquidityLevel:
    """Key liquidity level (PDH/PDL, session H/L, swing H/L, or EQH/EQL)."""

    price: float
    side: str                # "HIGH" (buy-side) or "LOW" (sell-side)
    label: str
    created_at: datetime
    is_equal: bool = False   # True for relative equal highs/lows
    swept: bool = False


@dataclass
class CISDSignal:
    """5m Change in State of Delivery (body-close confirmation)."""

    direction: Bias          # BULLISH or BEARISH
    confirmed_at: datetime
    ob_body_level: float     # Limit entry level (OB candle body high/low, wicks ignored)
    sl_price: float          # Previous 5m swing low/high
    aoi_source: Optional[AOI] = None
    is_momentum: bool = False


@dataclass
class Trade:
    """Simulated or live trade lifecycle."""

    trade_id: int
    symbol: str
    session: str
    direction: Bias
    entry_price: float       # Limit price from CISD OB body
    initial_sl: float
    current_sl: float
    tp_price: float
    risk_pct: float
    risk_amount: float
    size: float              # Position units (risk_amount / abs(entry - initial_sl))
    placed_at: datetime
    expires_at: datetime
    status: TradeStatus = TradeStatus.PENDING
    filled_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    costs: float = 0.0
    pnl: float = 0.0
    rr_realized: float = 0.0
    same_dir_cisds: int = 0
    opp_consec_cisds: int = 0
    moved_to_be: bool = False
    is_news_trade: bool = False
    news_close_at: Optional[datetime] = None
    notes: str = ""

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.initial_sl)

    def current_rr(self, price: float) -> float:
        dist = self.risk_distance
        if dist <= 0:
            return 0.0
        if self.direction == Bias.BULLISH:
            return (price - self.entry_price) / dist
        return (self.entry_price - price) / dist


@dataclass
class NewsEvent:
    """High-impact (red folder) economic news event."""

    timestamp: datetime      # Localized to America/New_York
    currency: str            # e.g., "USD", "GBP", "AUD", "JPY"
    title: str
    impact: str = "HIGH"
