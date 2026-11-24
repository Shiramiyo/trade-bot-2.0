"""Daily bias (green/red + Doji 1H trend) and ranging-day detection."""

import pandas as pd

from models import Bias


def determine_daily_bias(
    daily_df: pd.DataFrame,
    hourly_df: pd.DataFrame,
    current_time: pd.Timestamp,
    doji_body_ratio: float = 0.05,
) -> Bias:
    """
    Determine daily bias from the previous completed daily candle.
    - Green close (close > open) -> BULLISH
    - Red close (close < open) -> BEARISH
    - Doji (body <= doji_body_ratio * range) -> inspect previous day's 1H trend:
      clear HH+HL -> BULLISH, clear LH+LL -> BEARISH, unclear/ranging -> NONE.
    """
    completed_daily = daily_df[daily_df.index < current_time.normalize()]
    if completed_daily.empty:
        return Bias.NONE

    prev_day = completed_daily.iloc[-1]
    candle_range = float(prev_day["high"] - prev_day["low"])
    body = abs(float(prev_day["close"] - prev_day["open"]))

    is_doji = candle_range == 0 or (body / candle_range) <= doji_body_ratio
    if not is_doji:
        return Bias.BULLISH if prev_day["close"] > prev_day["open"] else Bias.BEARISH

    # Doji: inspect 1H structure from the previous day (up to 48h for clear swings)
    prev_day_start = completed_daily.index[-1]
    prev_1h = hourly_df[
        (hourly_df.index >= prev_day_start - pd.Timedelta(hours=24))
        & (hourly_df.index < current_time)
    ]
    return detect_1h_trend(prev_1h)


def detect_1h_trend(hourly_df: pd.DataFrame, swing_window: int = 2) -> Bias:
    """
    Check if 1H candles show a clear uptrend (HH + HL) or downtrend (LH + LL).
    Returns Bias.NONE if choppy / unclear.
    """
    if len(hourly_df) < swing_window * 2 + 5:
        return Bias.NONE

    highs = hourly_df["high"].values
    lows = hourly_df["low"].values
    swing_highs = []
    swing_lows = []

    for i in range(swing_window, len(hourly_df) - swing_window):
        if highs[i] == max(highs[i - swing_window : i + swing_window + 1]):
            swing_highs.append(float(highs[i]))
        if lows[i] == min(lows[i - swing_window : i + swing_window + 1]):
            swing_lows.append(float(lows[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        # Fallback: compare first-half vs second-half close slope if clean directional move
        closes = hourly_df["close"].values
        mid = len(closes) // 2
        if closes[-1] > closes[0] and max(highs[mid:]) > max(highs[:mid]) and min(lows[mid:]) >= min(lows[:mid]):
            return Bias.BULLISH
        if closes[-1] < closes[0] and min(lows[mid:]) < min(lows[:mid]) and max(highs[mid:]) <= max(highs[:mid]):
            return Bias.BEARISH
        return Bias.NONE

    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1] > swing_lows[-2]
    lh = swing_highs[-1] < swing_highs[-2]
    ll = swing_lows[-1] < swing_lows[-2]

    if hh and hl:
        return Bias.BULLISH
    if lh and ll:
        return Bias.BEARISH
    return Bias.NONE


def is_ranging_day(
    hourly_df: pd.DataFrame,
    current_time: pd.Timestamp,
    lookback_days: int = 3,
) -> bool:
    """
    Ranging day = a high and low made in the last 2-3 days have NOT yet been broken.
    Specifically: we find the highest high and lowest low formed 2-3 days ago
    (prior to today's session), and check if recent price (today / last 24h)
    is still strictly contained inside that [range_low, range_high].
    """
    window_start = current_time - pd.Timedelta(days=lookback_days)
    recent_1h = hourly_df[(hourly_df.index >= window_start) & (hourly_df.index < current_time)]
    if len(recent_1h) < 24:
        return False

    split_time = current_time - pd.Timedelta(days=1)
    prior_2_3d = recent_1h[recent_1h.index < split_time]
    last_1d = recent_1h[recent_1h.index >= split_time]

    if prior_2_3d.empty or last_1d.empty:
        return False

    range_high = float(prior_2_3d["high"].max())
    range_low = float(prior_2_3d["low"].min())

    # Neither the high nor the low from the prior 2-3 days has been broken
    return float(last_1d["high"].max()) <= range_high and float(last_1d["low"].min()) >= range_low
