"""5-minute CISD (Change in State of Delivery) and momentum OB/FVG detection."""

from typing import Optional
import pandas as pd

from indicators.aoi import detect_fvgs, detect_order_blocks
from models import AOI, Bias, CISDSignal


def detect_5m_cisd(
    df_5m: pd.DataFrame,
    direction: Bias,
    aoi_source: Optional[AOI] = None,
    swing_lookback: int = 3,
    is_momentum: bool = False,
    min_body_ratio: float = 0.0,
    require_sweep: bool = False,
) -> Optional[CISDSignal]:
    """
    Detect a 5m CISD at the latest closed 5m candle (df_5m.iloc[-1]).

    Exact user rules:
    - Must be a BODY CLOSE (wicks do NOT count).
    - Optional quality filters:
      - min_body_ratio: CISD candle body / total range >= min_body_ratio (displacement).
      - require_sweep: OB leg swept a prior 5m swing low (bullish) or high (bearish).
    """
    if len(df_5m) < max(12, swing_lookback + 6) or direction == Bias.NONE:
        return None

    last = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]

    c_range = float(last["high"] - last["low"])
    c_body = abs(float(last["close"] - last["open"]))
    if min_body_ratio > 0 and (c_range <= 0 or (c_body / c_range) < min_body_ratio):
        return None

    if direction == Bias.BULLISH:
        # Latest candle must close bullish
        if last["close"] <= last["open"]:
            return None

        # Find the consecutive down-close candles (the bearish OB) just before the up-move
        ob_candles = _find_recent_opposing_run(df_5m.iloc[:-1], opposing_bullish=False)
        if ob_candles.empty:
            return None

        if require_sweep and not _swept_prior_swing(df_5m, ob_candles, is_high=False):
            return None

        # Highest body of the bearish OB (wicks ignored)
        ob_body_level = float(
            ob_candles.apply(lambda r: max(r["open"], r["close"]), axis=1).max()
        )

        # Body close strictly above OB body level, and prior candle had not already closed above
        if float(last["close"]) > ob_body_level and float(prev["close"]) <= ob_body_level:
            sl_price = _previous_swing_extreme(df_5m, is_high=False, lookback=swing_lookback)
            if sl_price >= ob_body_level:
                return None
            return CISDSignal(
                direction=Bias.BULLISH,
                confirmed_at=df_5m.index[-1].to_pydatetime(),
                ob_body_level=ob_body_level,
                sl_price=sl_price,
                aoi_source=aoi_source,
                is_momentum=is_momentum,
            )

    elif direction == Bias.BEARISH:
        # Latest candle must close bearish
        if last["close"] >= last["open"]:
            return None

        # Find the consecutive up-close candles (the bullish OB) just before the down-move
        ob_candles = _find_recent_opposing_run(df_5m.iloc[:-1], opposing_bullish=True)
        if ob_candles.empty:
            return None

        if require_sweep and not _swept_prior_swing(df_5m, ob_candles, is_high=True):
            return None

        # Lowest body of the bullish OB (wicks ignored)
        ob_body_level = float(
            ob_candles.apply(lambda r: min(r["open"], r["close"]), axis=1).min()
        )

        # Body close strictly below OB body level, and prior candle had not already closed below
        if float(last["close"]) < ob_body_level and float(prev["close"]) >= ob_body_level:
            sl_price = _previous_swing_extreme(df_5m, is_high=True, lookback=swing_lookback)
            if sl_price <= ob_body_level:
                return None
            return CISDSignal(
                direction=Bias.BEARISH,
                confirmed_at=df_5m.index[-1].to_pydatetime(),
                ob_body_level=ob_body_level,
                sl_price=sl_price,
                aoi_source=aoi_source,
                is_momentum=is_momentum,
            )

    return None


def _swept_prior_swing(
    df_5m: pd.DataFrame,
    ob_candles: pd.DataFrame,
    is_high: bool,
    lookback_bars: int = 20,
) -> bool:
    """Return True if the OB leg swept below/above the prior 5m swing low/high."""
    first_ob_ts = ob_candles.index[0]
    prior = df_5m[df_5m.index < first_ob_ts].tail(lookback_bars)
    if len(prior) < 4:
        return False
    if is_high:
        return float(ob_candles["high"].max()) > float(prior["high"].max())
    return float(ob_candles["low"].min()) < float(prior["low"].min())


def _find_recent_opposing_run(df_prior: pd.DataFrame, opposing_bullish: bool, max_scan: int = 12) -> pd.DataFrame:
    """
    Scan backwards from the end of df_prior (skipping at most 2 transitional candles)
    to collect the contiguous run of opposing candles forming the OB.
    """
    recent = df_prior.tail(max_scan)
    if recent.empty:
        return recent

    rows = []
    started = False
    for idx in range(len(recent) - 1, -1, -1):
        row = recent.iloc[idx]
        is_match = (row["close"] > row["open"]) if opposing_bullish else (row["close"] < row["open"])
        if is_match:
            started = True
            rows.append(recent.iloc[idx : idx + 1])
        elif started:
            break
        elif len(recent) - 1 - idx >= 2:
            break

    if not rows:
        return pd.DataFrame(columns=recent.columns)
    return pd.concat(reversed(rows))


def _previous_swing_extreme(df_5m: pd.DataFrame, is_high: bool, lookback: int = 3) -> float:
    """Return the full pre-CISD 5m swing high/low over the last 24 bars (2 hours)."""
    recent = df_5m.tail(max(24, lookback * 8))
    if is_high:
        return float(recent["high"].max())
    return float(recent["low"].min())


def detect_5m_momentum_aoi(df_5m: pd.DataFrame, bias: Bias) -> Optional[AOI]:
    """
    Fallback for 'price already moved before our trading hour':
    look for a recent unmitigated 5m FVG or 5m OB aligned with daily bias
    that the current 5m candle is retracing into.
    """
    if len(df_5m) < 10 or bias == Bias.NONE:
        return None

    window = df_5m.tail(36)  # last 3 hours of 5m candles
    last = window.iloc[-1]
    candidates = detect_fvgs(window.iloc[:-1], "5m") + detect_order_blocks(window.iloc[:-1], "5m")
    aligned = [a for a in candidates if a.direction == bias and not a.mitigated]

    for aoi in reversed(aligned):
        if aoi.contains_price(float(last["low"]), float(last["high"])):
            return aoi
    return None
