"""1H Areas of Interest (FVG & Order Block) and Liquidity Level detection."""

from typing import List
import numpy as np
import pandas as pd

from models import AOI, AOIType, Bias, LiquidityLevel


def _is_aoi_invalidated(
    highs_after: np.ndarray,
    lows_after: np.ndarray,
    closes_after: np.ndarray,
    bottom: float,
    top: float,
    is_bullish: bool,
    max_touches: int = 2,
) -> bool:
    """
    Mentor rule: An OB or FVG is invalidated if price closes completely through it,
    OR if it gets filled/touched more than twice (> 2 separate visits without bouncing).
    """
    if len(closes_after) == 0:
        return False
    if is_bullish and bool(np.any(closes_after < bottom)):
        return True
    if not is_bullish and bool(np.any(closes_after > top)):
        return True

    # Count distinct visits into [bottom, top]
    in_zone = (highs_after >= bottom) & (lows_after <= top)
    touches = 0
    was_in = False
    for flag in in_zone:
        if flag and not was_in:
            touches += 1
            if touches > max_touches:
                return True
        was_in = bool(flag)
    return False


def detect_fvgs(df: pd.DataFrame, timeframe: str = "1H") -> List[AOI]:
    """
    Official 3-candle Fair Value Gap (FVG) definition:
    - Bullish FVG: candle[i-2].high < candle[i].low (gap between c[i-2].high and c[i].low)
    - Bearish FVG: candle[i-2].low > candle[i].high (gap between c[i].high and c[i-2].low)
    Marks FVG as mitigated if a subsequent candle closes completely through the far edge.
    """
    fvgs: List[AOI] = []
    if len(df) < 3:
        return fvgs

    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values
    times = df.index

    for i in range(2, len(df)):
        # Bullish FVG
        if highs[i - 2] < lows[i]:
            bottom = float(highs[i - 2])
            top = float(lows[i])
            mitigated = _is_aoi_invalidated(highs[i + 1 :], lows[i + 1 :], closes[i + 1 :], bottom, top, True)
            fvgs.append(
                AOI(
                    aoi_type=AOIType.FVG,
                    direction=Bias.BULLISH,
                    top=top,
                    bottom=bottom,
                    body_top=top,
                    body_bottom=bottom,
                    created_at=times[i].to_pydatetime(),
                    timeframe=timeframe,
                    mitigated=mitigated,
                )
            )
        # Bearish FVG
        elif lows[i - 2] > highs[i]:
            top = float(lows[i - 2])
            bottom = float(highs[i])
            mitigated = _is_aoi_invalidated(highs[i + 1 :], lows[i + 1 :], closes[i + 1 :], bottom, top, False)
            fvgs.append(
                AOI(
                    aoi_type=AOIType.FVG,
                    direction=Bias.BEARISH,
                    top=top,
                    bottom=bottom,
                    body_top=top,
                    body_bottom=bottom,
                    created_at=times[i].to_pydatetime(),
                    timeframe=timeframe,
                    mitigated=mitigated,
                )
            )

    return fvgs


def detect_order_blocks(df: pd.DataFrame, timeframe: str = "1H", swing_lookback: int = 5) -> List[AOI]:
    """
    Valid ICT Order Block definition:
    - Bullish OB: Last down-close candle (close < open) immediately before an impulsive
      upward move that BOTH creates an FVG (imbalance) AND breaks a prior swing high.
    - Bearish OB: Last up-close candle (close > open) immediately before an impulsive
      downward move that BOTH creates an FVG (imbalance) AND breaks a prior swing low.
    Marks OB as mitigated if a subsequent candle closes beyond the OB's far edge.
    """
    obs: List[AOI] = []
    if len(df) < swing_lookback + 4:
        return obs

    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df.index

    for i in range(swing_lookback, len(df) - 2):
        c_open, c_high, c_low, c_close = opens[i], highs[i], lows[i], closes[i]
        prior_high = float(np.max(highs[i - swing_lookback : i]))
        prior_low = float(np.min(lows[i - swing_lookback : i]))
        next_end = min(len(df), i + 4)

        # Bullish OB candidate: down-close candle + FVG imbalance + break of prior swing high
        if c_close < c_open:
            has_fvg = highs[i] < lows[i + 2]
            breaks_swing = float(np.max(closes[i + 1 : next_end])) > prior_high
            if has_fvg and breaks_swing and closes[i + 1] > opens[i + 1]:
                mitigated = _is_aoi_invalidated(highs[i + 2 :], lows[i + 2 :], closes[i + 2 :], c_low, c_high, True)
                obs.append(
                    AOI(
                        aoi_type=AOIType.OB,
                        direction=Bias.BULLISH,
                        top=float(c_high),
                        bottom=float(c_low),
                        body_top=float(max(c_open, c_close)),
                        body_bottom=float(min(c_open, c_close)),
                        created_at=times[i].to_pydatetime(),
                        timeframe=timeframe,
                        mitigated=mitigated,
                    )
                )

        # Bearish OB candidate: up-close candle + FVG imbalance + break of prior swing low
        elif c_close > c_open:
            has_fvg = lows[i] > highs[i + 2]
            breaks_swing = float(np.min(closes[i + 1 : next_end])) < prior_low
            if has_fvg and breaks_swing and closes[i + 1] < opens[i + 1]:
                mitigated = _is_aoi_invalidated(highs[i + 2 :], lows[i + 2 :], closes[i + 2 :], c_low, c_high, False)
                obs.append(
                    AOI(
                        aoi_type=AOIType.OB,
                        direction=Bias.BEARISH,
                        top=float(c_high),
                        bottom=float(c_low),
                        body_top=float(max(c_open, c_close)),
                        body_bottom=float(min(c_open, c_close)),
                        created_at=times[i].to_pydatetime(),
                        timeframe=timeframe,
                        mitigated=mitigated,
                    )
                )

    return obs


def get_active_1h_aois(
    hourly_df: pd.DataFrame,
    current_time: pd.Timestamp,
    bias: Bias,
) -> List[AOI]:
    """
    Find unmitigated 1H FVGs and OBs aligned with daily bias.
    Rule: Look first at the previous 1 day; only if nothing is found there,
    look back at the day before yesterday (2 days ago max).
    """
    if bias == Bias.NONE:
        return []

    day1_start = current_time - pd.Timedelta(days=1)
    day2_start = current_time - pd.Timedelta(days=2)

    df_1d = hourly_df[(hourly_df.index >= day1_start) & (hourly_df.index < current_time)]
    aois_1d = _extract_unmitigated(df_1d, bias)
    if aois_1d:
        return aois_1d

    df_2d = hourly_df[(hourly_df.index >= day2_start) & (hourly_df.index < current_time)]
    return _extract_unmitigated(df_2d, bias)


def _extract_unmitigated(df: pd.DataFrame, bias: Bias) -> List[AOI]:
    if df.empty:
        return []
    all_aois = detect_fvgs(df, "1H") + detect_order_blocks(df, "1H")
    return [a for a in all_aois if a.direction == bias and not a.mitigated]


def detect_liquidity_levels(
    hourly_df: pd.DataFrame,
    current_time: pd.Timestamp,
    eq_tolerance_atr: float = 0.15,
    lookback_days: int = 3,
) -> List[LiquidityLevel]:
    """
    Identify clear buy-side (HIGH) and sell-side (LOW) liquidity levels:
    - Previous Day High / Low (PDH / PDL)
    - 1H Swing Highs / Lows
    - Relative Equal Highs / Equal Lows (EQH / EQL) within ATR tolerance
    """
    levels: List[LiquidityLevel] = []
    window = hourly_df[
        (hourly_df.index >= current_time - pd.Timedelta(days=lookback_days))
        & (hourly_df.index < current_time)
    ]
    if len(window) < 10:
        return levels

    tr = np.maximum(
        window["high"].values - window["low"].values,
        np.maximum(
            np.abs(window["high"].values - np.roll(window["close"].values, 1)),
            np.abs(window["low"].values - np.roll(window["close"].values, 1)),
        ),
    )
    atr = float(np.mean(tr[1:])) if len(tr) > 1 else 0.0
    tol = atr * eq_tolerance_atr

    # Previous 1 day high / low
    prev_1d = window[window.index >= current_time - pd.Timedelta(days=1)]
    if not prev_1d.empty:
        levels.append(
            LiquidityLevel(
                price=float(prev_1d["high"].max()),
                side="HIGH",
                label="PDH",
                created_at=prev_1d["high"].idxmax().to_pydatetime(),
            )
        )
        levels.append(
            LiquidityLevel(
                price=float(prev_1d["low"].min()),
                side="LOW",
                label="PDL",
                created_at=prev_1d["low"].idxmin().to_pydatetime(),
            )
        )

    # 1H swing highs and lows + Relative Equal Highs / Lows
    highs = window["high"].values
    lows = window["low"].values
    times = window.index
    swing_highs = []
    swing_lows = []

    for i in range(2, len(window) - 2):
        if highs[i] == max(highs[i - 2 : i + 3]):
            swing_highs.append((float(highs[i]), times[i].to_pydatetime()))
        if lows[i] == min(lows[i - 2 : i + 3]):
            swing_lows.append((float(lows[i]), times[i].to_pydatetime()))

    for idx, (price, ts) in enumerate(swing_highs):
        is_eq = any(abs(price - p2) <= tol for j, (p2, _) in enumerate(swing_highs) if j != idx)
        levels.append(
            LiquidityLevel(
                price=price,
                side="HIGH",
                label="EQH" if is_eq else "1H_SWING_HIGH",
                created_at=ts,
                is_equal=is_eq,
            )
        )

    for idx, (price, ts) in enumerate(swing_lows):
        is_eq = any(abs(price - p2) <= tol for j, (p2, _) in enumerate(swing_lows) if j != idx)
        levels.append(
            LiquidityLevel(
                price=price,
                side="LOW",
                label="EQL" if is_eq else "1H_SWING_LOW",
                created_at=ts,
                is_equal=is_eq,
            )
        )

    return levels
