"""
4-Year Bot-Native Strategy Arena (2023-01-01 to 2026-11-13) on NQ & XAUUSD.

Compares Mechanical SMC baselines against bot-native quantitative strategies
using unified MT5-calibrated OHLC data (`data_4y/`), identical 1% risk sizing,
conservative intra-bar OHLC execution, and realistic spread/slippage/commissions.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from config import SYMBOL_COSTS
from data_loader import NY_TZ, load_from_csv
from models import Bias, Trade, TradeStatus
from report import compute_metrics
from smc_bot import evaluate_15m_smc_signal, run_smc_backtest


AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}


@dataclass
class ArenaTrade:
    symbol: str
    direction: Bias
    order_type: str  # "market", "limit", "stop"
    entry_price: float
    initial_sl: float
    current_sl: float
    tp_price: Optional[float]
    risk_amount: float
    size: float
    placed_at: pd.Timestamp
    expires_at: Optional[pd.Timestamp]
    status: str = "PENDING"  # "PENDING", "OPEN", "CLOSED"
    filled_at: Optional[pd.Timestamp] = None
    closed_at: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0
    costs: float = 0.0
    exit_at_eod: bool = False
    trail_atr_mult: float = 0.0
    exit_on_rsi2: bool = False
    use_supertrend_trail: bool = False


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr = compute_atr(df, period).replace(0.0, np.nan)
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean() / atr
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)


def compute_rsi(close: pd.Series, period: int = 2) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def compute_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> Tuple[pd.Series, pd.Series]:
    atr = compute_atr(df, period).to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    hl2 = 0.5 * (high + low)
    n = len(df)

    final_up = hl2 + mult * atr
    final_dn = hl2 - mult * atr
    direction = np.ones(n, dtype=int)
    st_line = np.zeros(n, dtype=float)

    for i in range(1, n):
        up_basic = hl2[i] + mult * atr[i]
        dn_basic = hl2[i] - mult * atr[i]
        final_up[i] = up_basic if (up_basic < final_up[i - 1] or close[i - 1] > final_up[i - 1]) else final_up[i - 1]
        final_dn[i] = dn_basic if (dn_basic > final_dn[i - 1] or close[i - 1] < final_dn[i - 1]) else final_dn[i - 1]

        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < final_dn[i] else 1
        else:
            direction[i] = 1 if close[i] > final_up[i] else -1
        st_line[i] = final_dn[i] if direction[i] == 1 else final_up[i]

    return pd.Series(direction, index=df.index), pd.Series(st_line, index=df.index)


def _is_year_end_blackout(ts: pd.Timestamp) -> bool:
    d = ts.date()
    return (date(d.year, 12, 31) - d).days < 5


def _estimate_cost(symbol: str, entry: float, size: float, has_entry_slip: bool, assume_sl_slip: bool = True) -> float:
    sc = SYMBOL_COSTS.get(symbol)
    if sc is None:
        return 0.0
    slips = (1.0 if has_entry_slip else 0.0) + (1.0 if assume_sl_slip else 0.0)
    return (sc.spread + slips * sc.slippage_per_side) * size + 2.0 * sc.commission_rate * entry * size


def _settle(trade: ArenaTrade, exit_price: float, ts: pd.Timestamp, reason: str, exit_slip: bool) -> float:
    trade.status = "CLOSED"
    trade.exit_price = exit_price
    trade.closed_at = ts
    trade.exit_reason = reason
    gross = (
        (exit_price - trade.entry_price) * trade.size
        if trade.direction == Bias.BULLISH
        else (trade.entry_price - exit_price) * trade.size
    )
    sc = SYMBOL_COSTS.get(trade.symbol)
    costs = 0.0
    if sc is not None:
        entry_slip = sc.slippage_per_side if trade.order_type in ("market", "stop") else 0.0
        ex_slip = sc.slippage_per_side if exit_slip else 0.0
        costs = (sc.spread + entry_slip + ex_slip) * trade.size + 2.0 * sc.commission_rate * trade.entry_price * trade.size
    trade.costs = costs
    trade.pnl = gross - costs
    return trade.pnl


def _add_causal_session_box(df: pd.DataFrame, start_t: time, end_t: time, prefix: str) -> None:
    """Attach strictly causal prior-session box high/low columns (available only after `end_t`)."""
    t_arr = df.index.time
    overnight = start_t > end_t
    if overnight:
        s_dates = np.where(t_arr >= start_t, (df.index + pd.Timedelta(days=1)).date, df.index.date)
        in_box = (t_arr >= start_t) | (t_arr < end_t)
        after_box = (t_arr >= end_t) & (t_arr < start_t)
    else:
        s_dates = df.index.date
        in_box = (t_arr >= start_t) & (t_arr < end_t)
        after_box = t_arr >= end_t

    tmp = pd.DataFrame({"sdate": s_dates, "high": df["high"].to_numpy(), "low": df["low"].to_numpy()}, index=df.index)
    box_stats = tmp[in_box].groupby("sdate").agg(hi=("high", "max"), lo=("low", "min"))
    mapped = pd.Series(s_dates, index=df.index).map(box_stats["hi"]), pd.Series(s_dates, index=df.index).map(box_stats["lo"])
    df[f"{prefix}_hi"] = np.where(after_box, mapped[0].to_numpy(), np.nan)
    df[f"{prefix}_lo"] = np.where(after_box, mapped[1].to_numpy(), np.nan)


def prepare_symbol_frames(df_15m: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Build aligned 15m, 30m, 1H, 4H, and 1D indicators from calibrated 15m data."""
    df15 = df_15m.copy()
    df30 = df_15m.resample("30min").agg(AGG).dropna()
    df1h = df_15m.resample("1h").agg(AGG).dropna()
    df4h = df_15m.resample("4h").agg(AGG).dropna()

    naive = df_15m.copy()
    naive.index = naive.index.tz_localize(None)
    df1d = (naive.shift(-17, freq="h").resample("1D").agg(AGG).dropna()).shift(17, freq="h")
    df1d.index = df1d.index.tz_localize(NY_TZ, ambiguous="infer", nonexistent="shift_forward")
    df1d["ema20"] = df1d["close"].ewm(span=20, adjust=False).mean()
    df1d["ema50"] = df1d["close"].ewm(span=50, adjust=False).mean()

    for d in (df15, df30, df1h):
        d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
        d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
        d["ema200"] = d["close"].ewm(span=200, adjust=False).mean()
        d["atr14"] = compute_atr(d, 14)
        d["adx14"] = compute_adx(d, 14)
        d["rsi2"] = compute_rsi(d["close"], 2)
        sma20 = d["close"].rolling(20).mean()
        std20 = d["close"].rolling(20).std()
        d["bb_mid"] = sma20
        d["z20"] = (d["close"] - sma20) / std20.replace(0.0, np.nan)

    _add_causal_session_box(df15, time(9, 30), time(10, 0), "orb30")
    _add_causal_session_box(df15, time(9, 30), time(9, 45), "orb15")
    _add_causal_session_box(df15, time(3, 0), time(3, 30), "ldn30")
    _add_causal_session_box(df15, time(18, 0), time(2, 0), "asian")
    _add_causal_session_box(df30, time(18, 0), time(2, 0), "asian")

    df1h["don20_hi"] = df1h["high"].rolling(20).max().shift(1)
    df1h["don20_lo"] = df1h["low"].rolling(20).min().shift(1)
    df1h["st_dir"], df1h["st_line"] = compute_supertrend(df1h, period=10, mult=3.0)
    df4h["ema20"] = df4h["close"].ewm(span=20, adjust=False).mean()
    df4h["ema50"] = df4h["close"].ewm(span=50, adjust=False).mean()

    return {"15m": df15, "30m": df30, "1h": df1h, "4h": df4h, "1d": df1d}


def _to_report_trades(trades: List[ArenaTrade]) -> List[Trade]:
    out: List[Trade] = []
    for idx, t in enumerate(trades, 1):
        out.append(
            Trade(
                trade_id=idx,
                symbol=t.symbol,
                session="ARENA",
                direction=t.direction,
                entry_price=t.entry_price,
                initial_sl=t.initial_sl,
                current_sl=t.current_sl,
                tp_price=t.tp_price or 0.0,
                risk_pct=0.01,
                risk_amount=t.risk_amount,
                size=t.size,
                placed_at=t.placed_at.to_pydatetime(),
                expires_at=t.expires_at.to_pydatetime() if t.expires_at is not None else t.placed_at.to_pydatetime(),
                status=TradeStatus.CLOSED if t.status == "CLOSED" else TradeStatus.CANCELLED,
                filled_at=t.filled_at.to_pydatetime() if t.filled_at is not None else None,
                closed_at=t.closed_at.to_pydatetime() if t.closed_at is not None else None,
                exit_price=t.exit_price,
                exit_reason=t.exit_reason,
                pnl=t.pnl,
                costs=t.costs,
                rr_realized=t.pnl / t.risk_amount if t.risk_amount > 0 else 0.0,
            )
        )
    return out


def run_arena_strategy(
    frames_by_sym: Dict[str, Dict[str, pd.DataFrame]],
    ltf: str,
    signal_fn: Callable,
    start_date: str = "2023-01-01",
    end_date: str = "2026-12-31",
    risk_pct: float = 0.01,
    initial_capital: float = 100_000.0,
    max_cost_ratio: float = 0.20,
    one_per_day: bool = False,
) -> List[ArenaTrade]:
    """Generic causal bar-by-bar portfolio simulator for bot-native strategies."""
    start_ts = pd.Timestamp(start_date, tz=NY_TZ)
    end_ts = pd.Timestamp(end_date, tz=NY_TZ) + pd.Timedelta(days=1)

    all_times = sorted(
        {
            ts
            for frames in frames_by_sym.values()
            for ts in frames[ltf].index
            if start_ts <= ts < end_ts
        }
    )
    pos_map = {sym: {ts: i for i, ts in enumerate(frames[ltf].index)} for sym, frames in frames_by_sym.items()}

    equity = initial_capital
    trades: List[ArenaTrade] = []
    active: Dict[str, ArenaTrade] = {}
    traded_days: Dict[str, set] = {sym: set() for sym in frames_by_sym}

    for ts in all_times:
        # 1. Manage existing PENDING & OPEN trades first (O(1) active trades)
        for sym in list(active.keys()):
            tr = active[sym]
            i = pos_map[sym].get(ts)
            if i is None:
                continue
            row = frames_by_sym[sym][ltf].iloc[i]
            hi, lo, op, cl = float(row["high"]), float(row["low"]), float(row["open"]), float(row["close"])

            if tr.status == "PENDING":
                if tr.expires_at is not None and ts >= tr.expires_at:
                    tr.status = "CANCELLED"
                    tr.exit_reason = "EXPIRED"
                    tr.pnl = 0.0
                    del active[sym]
                    continue
                if tr.order_type == "limit":
                    hit = (lo <= tr.entry_price) if tr.direction == Bias.BULLISH else (hi >= tr.entry_price)
                    if hit:
                        tr.status = "OPEN"
                        tr.filled_at = ts
                        if (tr.direction == Bias.BULLISH and lo <= tr.current_sl) or (
                            tr.direction == Bias.BEARISH and hi >= tr.current_sl
                        ):
                            equity += _settle(tr, tr.current_sl, ts, "SL_SAME_BAR", exit_slip=True)
                            del active[sym]
                            continue
                elif tr.order_type == "stop":
                    hit = (hi >= tr.entry_price) if tr.direction == Bias.BULLISH else (lo <= tr.entry_price)
                    if hit:
                        if tr.direction == Bias.BULLISH and op > tr.entry_price:
                            tr.entry_price = op
                        elif tr.direction == Bias.BEARISH and op < tr.entry_price:
                            tr.entry_price = op
                        tr.status = "OPEN"
                        tr.filled_at = ts
                        if (tr.direction == Bias.BULLISH and lo <= tr.current_sl) or (
                            tr.direction == Bias.BEARISH and hi >= tr.current_sl
                        ):
                            equity += _settle(tr, tr.current_sl, ts, "SL_SAME_BAR", exit_slip=True)
                            del active[sym]
                            continue
                continue

            if tr.status == "OPEN":
                if tr.direction == Bias.BULLISH:
                    if lo <= tr.current_sl:
                        equity += _settle(tr, tr.current_sl, ts, "SL", exit_slip=True)
                        del active[sym]
                        continue
                    if tr.tp_price is not None and hi >= tr.tp_price:
                        equity += _settle(tr, tr.tp_price, ts, "TP", exit_slip=False)
                        del active[sym]
                        continue
                else:
                    if hi >= tr.current_sl:
                        equity += _settle(tr, tr.current_sl, ts, "SL", exit_slip=True)
                        del active[sym]
                        continue
                    if tr.tp_price is not None and lo <= tr.tp_price:
                        equity += _settle(tr, tr.tp_price, ts, "TP", exit_slip=False)
                        del active[sym]
                        continue

                if tr.exit_at_eod and ts.time() >= time(15, 45):
                    equity += _settle(tr, cl, ts, "EOD", exit_slip=True)
                    del active[sym]
                    continue

                if tr.exit_on_rsi2:
                    rsi2 = float(row["rsi2"])
                    if (tr.direction == Bias.BULLISH and rsi2 >= 70.0) or (
                        tr.direction == Bias.BEARISH and rsi2 <= 30.0
                    ):
                        equity += _settle(tr, cl, ts, "RSI2_EXIT", exit_slip=True)
                        del active[sym]
                        continue

                if tr.trail_atr_mult > 0:
                    atr = float(row["atr14"])
                    if tr.direction == Bias.BULLISH:
                        tr.current_sl = max(tr.current_sl, cl - tr.trail_atr_mult * atr)
                    else:
                        tr.current_sl = min(tr.current_sl, cl + tr.trail_atr_mult * atr)

                if tr.use_supertrend_trail:
                    st_dir = int(row["st_dir"])
                    st_line = float(row["st_line"])
                    if tr.direction == Bias.BULLISH:
                        if st_dir == -1:
                            equity += _settle(tr, cl, ts, "ST_FLIP", exit_slip=True)
                            del active[sym]
                            continue
                        tr.current_sl = max(tr.current_sl, st_line)
                    else:
                        if st_dir == 1:
                            equity += _settle(tr, cl, ts, "ST_FLIP", exit_slip=True)
                            del active[sym]
                            continue
                        tr.current_sl = min(tr.current_sl, st_line)

        if ts.weekday() >= 5 or _is_year_end_blackout(ts):
            continue

        # 2. Scan new entries
        for sym, frames in frames_by_sym.items():
            if sym in active:
                continue
            i = pos_map[sym].get(ts)
            if i is None or i < 30:
                continue
            if one_per_day and ts.date() in traded_days[sym]:
                continue

            now_close = ts + LTF_DELTAS[ltf]
            spec = signal_fn(sym, frames, i, ts, now_close)
            if spec is None:
                continue

            entry_price = float(spec["entry"])
            sl_price = float(spec["sl"])
            risk_dist = abs(entry_price - sl_price)
            if risk_dist <= 0:
                continue
            risk_amount = equity * risk_pct
            size = risk_amount / risk_dist
            order_type = spec["order_type"]
            est_cost = _estimate_cost(sym, entry_price, size, has_entry_slip=(order_type != "limit"))
            if est_cost > risk_amount * max_cost_ratio:
                continue

            tr = ArenaTrade(
                symbol=sym,
                direction=spec["direction"],
                order_type=order_type,
                entry_price=entry_price,
                initial_sl=sl_price,
                current_sl=sl_price,
                tp_price=spec.get("tp"),
                risk_amount=risk_amount,
                size=size,
                placed_at=ts,
                expires_at=spec.get("expires_at"),
                status="OPEN" if order_type == "market" else "PENDING",
                filled_at=ts if order_type == "market" else None,
                exit_at_eod=spec.get("exit_at_eod", False),
                trail_atr_mult=spec.get("trail_atr_mult", 0.0),
                exit_on_rsi2=spec.get("exit_on_rsi2", False),
                use_supertrend_trail=spec.get("use_supertrend_trail", False),
            )
            trades.append(tr)
            active[sym] = tr
            if one_per_day:
                traded_days[sym].add(ts.date())

    return trades


# ---------------------------------------------------------------------------
# Strategy Signal Functions
# ---------------------------------------------------------------------------

LTF_DELTAS: Dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
}


def _last_closed_htf_idx(htf_df: pd.DataFrame, now_close: pd.Timestamp, htf_delta: timedelta) -> int:
    """Return index of most recent HTF bar whose close <= `now_close` (strict zero-lookahead)."""
    cutoff = now_close - htf_delta
    return int(htf_df.index.searchsorted(cutoff, side="right") - 1)


def _h1_trend(frames: Dict[str, pd.DataFrame], now_close: pd.Timestamp) -> Optional[Bias]:
    h1 = frames["1h"]
    idx = _last_closed_htf_idx(h1, now_close, timedelta(hours=1))
    if idx < 200:
        return None
    row = h1.iloc[idx]
    if row["ema50"] > row["ema200"] and row["close"] > row["ema50"]:
        return Bias.BULLISH
    if row["ema50"] < row["ema200"] and row["close"] < row["ema50"]:
        return Bias.BEARISH
    return None


def sig_orb_30m_stop_eod(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    df = frames["15m"]
    row = df.iloc[i]
    hi, lo = float(row["orb30_hi"]), float(row["orb30_lo"])
    if pd.isna(hi) or not (time(10, 0) <= ts.time() < time(13, 0)):
        return None
    rng = hi - lo
    h1_idx = _last_closed_htf_idx(frames["1h"], now_close, timedelta(hours=1))
    if h1_idx < 200:
        return None
    atr1h = float(frames["1h"].iloc[h1_idx]["atr14"])
    if rng <= 0.2 * atr1h or rng > 2.5 * atr1h:
        return None
    trend = _h1_trend(frames, now_close)
    cl = float(row["close"])
    if trend == Bias.BULLISH and cl <= hi:
        return {"direction": Bias.BULLISH, "order_type": "stop", "entry": hi, "sl": lo, "tp": hi + 2.0 * rng, "expires_at": ts + timedelta(hours=3), "exit_at_eod": True}
    if trend == Bias.BEARISH and cl >= lo:
        return {"direction": Bias.BEARISH, "order_type": "stop", "entry": lo, "sl": hi, "tp": lo - 2.0 * rng, "expires_at": ts + timedelta(hours=3), "exit_at_eod": True}
    return None


def sig_orb_15m_retest_limit(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    df = frames["15m"]
    row = df.iloc[i]
    hi, lo = float(row["orb15_hi"]), float(row["orb15_lo"])
    if pd.isna(hi) or not (time(9, 45) <= ts.time() < time(13, 30)) or hi <= lo:
        return None
    trend = _h1_trend(frames, now_close)
    prev_cl = float(df.iloc[i - 1]["close"])
    cl = float(row["close"])
    if trend == Bias.BULLISH and cl > hi and prev_cl <= hi:
        return {"direction": Bias.BULLISH, "order_type": "limit", "entry": hi, "sl": lo, "tp": hi + 2.0 * (hi - lo), "expires_at": ts + timedelta(hours=3), "exit_at_eod": True}
    if trend == Bias.BEARISH and cl < lo and prev_cl >= lo:
        return {"direction": Bias.BEARISH, "order_type": "limit", "entry": lo, "sl": hi, "tp": lo - 2.0 * (hi - lo), "expires_at": ts + timedelta(hours=3), "exit_at_eod": True}
    return None


def sig_orb_session_matched(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    """NQ trades NY 09:30-10:00 ORB; XAUUSD trades London 03:00-03:30 ORB."""
    df = frames["15m"]
    row = df.iloc[i]
    if sym == "NQ":
        hi, lo = float(row["orb30_hi"]), float(row["orb30_lo"])
        in_win = time(10, 0) <= ts.time() < time(13, 0)
    else:
        hi, lo = float(row["ldn30_hi"]), float(row["ldn30_lo"])
        in_win = time(3, 30) <= ts.time() < time(9, 30)
    if pd.isna(hi) or not in_win or hi <= lo:
        return None
    trend = _h1_trend(frames, now_close)
    cl = float(row["close"])
    mid = 0.5 * (hi + lo)
    if trend == Bias.BULLISH and cl > hi and float(df.iloc[i - 1]["close"]) <= hi:
        return {"direction": Bias.BULLISH, "order_type": "limit", "entry": hi, "sl": mid, "tp": hi + 2.0 * (hi - mid), "expires_at": ts + timedelta(hours=3), "exit_at_eod": True}
    if trend == Bias.BEARISH and cl < lo and float(df.iloc[i - 1]["close"]) >= lo:
        return {"direction": Bias.BEARISH, "order_type": "limit", "entry": lo, "sl": mid, "tp": lo - 2.0 * (mid - lo), "expires_at": ts + timedelta(hours=3), "exit_at_eod": True}
    return None


def sig_asian_sweep_fade(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    df = frames["15m"]
    row = df.iloc[i]
    hi, lo = float(row["asian_hi"]), float(row["asian_lo"])
    if pd.isna(hi) or not (time(2, 0) <= ts.time() < time(11, 0)):
        return None
    atr = float(row["atr14"])
    r_hi, r_lo, r_cl = float(row["high"]), float(row["low"]), float(row["close"])
    if r_hi > hi and r_cl < hi:
        sl = r_hi + 0.25 * atr
        return {"direction": Bias.BEARISH, "order_type": "market", "entry": r_cl, "sl": sl, "tp": r_cl - 2.0 * (sl - r_cl)}
    if r_lo < lo and r_cl > lo:
        sl = r_lo - 0.25 * atr
        return {"direction": Bias.BULLISH, "order_type": "market", "entry": r_cl, "sl": sl, "tp": r_cl + 2.0 * (r_cl - sl)}
    return None


def sig_asian_breakout_trend(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    df = frames["30m"]
    row = df.iloc[i]
    hi, lo = float(row["asian_hi"]), float(row["asian_lo"])
    if pd.isna(hi) or not (time(2, 0) <= ts.time() < time(12, 0)):
        return None
    trend = _h1_trend(frames, now_close)
    atr = float(row["atr14"])
    r_cl = float(row["close"])
    prev_cl = float(df.iloc[i - 1]["close"])
    if trend == Bias.BULLISH and r_cl > hi and prev_cl <= hi and float(row["adx14"]) > 18:
        sl = max(lo, r_cl - 2.0 * atr)
        return {"direction": Bias.BULLISH, "order_type": "market", "entry": r_cl, "sl": sl, "tp": r_cl + 2.0 * (r_cl - sl), "exit_at_eod": True}
    if trend == Bias.BEARISH and r_cl < lo and prev_cl >= lo and float(row["adx14"]) > 18:
        sl = min(hi, r_cl + 2.0 * atr)
        return {"direction": Bias.BEARISH, "order_type": "market", "entry": r_cl, "sl": sl, "tp": r_cl - 2.0 * (sl - r_cl), "exit_at_eod": True}
    return None


def sig_pdh_pdl_sweep_fade(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    if not (time(2, 0) <= ts.time() < time(14, 0)):
        return None
    df = frames["30m"]
    d1 = frames["1d"]
    d_idx = _last_closed_htf_idx(d1, now_close, timedelta(hours=24))
    if d_idx < 1:
        return None
    pd_bar = d1.iloc[d_idx]
    pdh, pdl = float(pd_bar["high"]), float(pd_bar["low"])
    row = df.iloc[i]
    atr = float(row["atr14"])
    r_hi, r_lo, r_cl = float(row["high"]), float(row["low"]), float(row["close"])
    if r_hi > pdh and r_cl < pdh and float(df.iloc[i - 1]["close"]) <= pdh:
        sl = r_hi + 0.25 * atr
        return {"direction": Bias.BEARISH, "order_type": "market", "entry": r_cl, "sl": sl, "tp": r_cl - 2.0 * (sl - r_cl)}
    if r_lo < pdl and r_cl > pdl and float(df.iloc[i - 1]["close"]) >= pdl:
        sl = r_lo - 0.25 * atr
        return {"direction": Bias.BULLISH, "order_type": "market", "entry": r_cl, "sl": sl, "tp": r_cl + 2.0 * (r_cl - sl)}
    return None


def sig_donchian_1h_trend(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    df = frames["1h"]
    if i < 205:
        return None
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    d4 = frames["4h"]
    i4 = _last_closed_htf_idx(d4, now_close, timedelta(hours=4))
    if i4 < 50:
        return None
    r4 = d4.iloc[i4]
    atr = float(row["atr14"])
    cl = float(row["close"])
    hi20, lo20 = float(row["don20_hi"]), float(row["don20_lo"])
    if pd.isna(hi20) or pd.isna(lo20) or atr <= 0:
        return None
    if cl > hi20 and float(prev["close"]) <= float(prev["don20_hi"]) and row["ema50"] > row["ema200"] and r4["ema20"] > r4["ema50"]:
        return {"direction": Bias.BULLISH, "order_type": "market", "entry": cl, "sl": cl - 2.0 * atr, "tp": None, "trail_atr_mult": 2.5}
    if cl < lo20 and float(prev["close"]) >= float(prev["don20_lo"]) and row["ema50"] < row["ema200"] and r4["ema20"] < r4["ema50"]:
        return {"direction": Bias.BEARISH, "order_type": "market", "entry": cl, "sl": cl + 2.0 * atr, "tp": None, "trail_atr_mult": 2.5}
    return None


def sig_supertrend_1h_adx(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    df = frames["1h"]
    if i < 205:
        return None
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    cl = float(row["close"])
    st_dir = int(row["st_dir"])
    prev_dir = int(prev["st_dir"])
    st_line = float(row["st_line"])
    if st_dir == 1 and prev_dir == -1 and cl > float(row["ema200"]) and float(row["adx14"]) > 20:
        return {"direction": Bias.BULLISH, "order_type": "market", "entry": cl, "sl": min(st_line, cl - 0.5 * float(row["atr14"])), "tp": None, "use_supertrend_trail": True}
    if st_dir == -1 and prev_dir == 1 and cl < float(row["ema200"]) and float(row["adx14"]) > 20:
        return {"direction": Bias.BEARISH, "order_type": "market", "entry": cl, "sl": max(st_line, cl + 0.5 * float(row["atr14"])), "tp": None, "use_supertrend_trail": True}
    return None


def sig_ema_pullback_30m(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    if not (time(2, 0) <= ts.time() < time(16, 0)):
        return None
    trend = _h1_trend(frames, now_close)
    if trend is None:
        return None
    df = frames["30m"]
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    if float(row["adx14"]) < 20:
        return None
    atr = float(row["atr14"])
    ema20 = float(row["ema20"])
    cl, op = float(row["close"]), float(row["open"])
    if trend == Bias.BULLISH and float(row["ema20"]) > float(row["ema50"]):
        touched = float(prev["low"]) <= float(prev["ema20"]) or float(row["low"]) <= ema20
        if touched and cl > ema20 and cl > op:
            sl = min(float(df.iloc[i - 3 : i + 1]["low"].min()), cl - 1.5 * atr)
            return {"direction": Bias.BULLISH, "order_type": "market", "entry": cl, "sl": sl, "tp": cl + 2.0 * (cl - sl)}
    if trend == Bias.BEARISH and float(row["ema20"]) < float(row["ema50"]):
        touched = float(prev["high"]) >= float(prev["ema20"]) or float(row["high"]) >= ema20
        if touched and cl < ema20 and cl < op:
            sl = max(float(df.iloc[i - 3 : i + 1]["high"].max()), cl + 1.5 * atr)
            return {"direction": Bias.BEARISH, "order_type": "market", "entry": cl, "sl": sl, "tp": cl - 2.0 * (sl - cl)}
    return None


def sig_mean_rev_bb_30m(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    if not (time(2, 0) <= ts.time() < time(16, 0)):
        return None
    df = frames["30m"]
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    if float(row["adx14"]) > 22 or pd.isna(prev["z20"]):
        return None
    atr = float(row["atr14"])
    cl = float(row["close"])
    mid = float(row["bb_mid"])
    if float(prev["z20"]) <= -2.0 and float(row["z20"]) > -2.0 and cl < mid:
        sl = float(df.iloc[i - 2 : i + 1]["low"].min()) - 0.5 * atr
        return {"direction": Bias.BULLISH, "order_type": "market", "entry": cl, "sl": sl, "tp": mid}
    if float(prev["z20"]) >= 2.0 and float(row["z20"]) < 2.0 and cl > mid:
        sl = float(df.iloc[i - 2 : i + 1]["high"].max()) + 0.5 * atr
        return {"direction": Bias.BEARISH, "order_type": "market", "entry": cl, "sl": sl, "tp": mid}
    return None


def sig_rsi2_trend_1h(sym: str, frames: Dict[str, pd.DataFrame], i: int, ts: pd.Timestamp, now_close: pd.Timestamp) -> Optional[dict]:
    df = frames["1h"]
    if i < 205:
        return None
    row = df.iloc[i]
    atr = float(row["atr14"])
    cl = float(row["close"])
    ema200 = float(row["ema200"])
    rsi2 = float(row["rsi2"])
    if cl > ema200 and rsi2 < 10.0:
        sl = cl - 2.0 * atr
        return {"direction": Bias.BULLISH, "order_type": "market", "entry": cl, "sl": sl, "tp": cl + 4.0 * atr, "exit_on_rsi2": True}
    if cl < ema200 and rsi2 > 90.0:
        sl = cl + 2.0 * atr
        return {"direction": Bias.BEARISH, "order_type": "market", "entry": cl, "sl": sl, "tp": cl - 4.0 * atr, "exit_on_rsi2": True}
    return None


def evaluate_suite(data_dir: Path, start: str = "2023-01-01", end: str = "2026-12-31") -> pd.DataFrame:
    raw_by_sym: Dict[str, pd.DataFrame] = {}
    frames_by_sym: Dict[str, Dict[str, pd.DataFrame]] = {}
    for sym in ("NQ", "XAUUSD"):
        df_base, _, _ = load_from_csv(data_dir / f"{sym}.csv")
        raw_by_sym[sym] = df_base
        frames_by_sym[sym] = prepare_symbol_frames(df_base)

    results = []

    def record(name: str, trades_obj: List[Trade], note: str) -> None:
        m_all = compute_metrics(trades_obj, 100_000.0)
        m_nq = compute_metrics([t for t in trades_obj if t.symbol == "NQ"], 100_000.0)
        m_xau = compute_metrics([t for t in trades_obj if t.symbol == "XAUUSD"], 100_000.0)
        closed = [t for t in trades_obj if t.status == TradeStatus.CLOSED]
        yr_pnls = {}
        for y in (2023, 2024, 2025, 2026):
            yr_pnls[y] = round(sum(t.pnl for t in closed if (t.closed_at or t.placed_at).year == y), 0)
        pos_years = sum(1 for y in (2023, 2024, 2025, 2026) if yr_pnls[y] > 0)
        ret_dd = round(m_all["return_pct"] / m_all["max_drawdown_pct"], 2) if m_all["max_drawdown_pct"] > 0 else 0.0
        results.append(
            {
                "strategy": name,
                "trades": m_all["total_trades"],
                "wr%": m_all["win_rate_pct"],
                "pf": m_all["profit_factor"],
                "net_pnl": m_all["net_pnl"],
                "ret%": m_all["return_pct"],
                "dd%": m_all["max_drawdown_pct"],
                "ret/dd": ret_dd,
                "costs": m_all.get("total_costs", 0.0),
                "nq_pnl": m_nq["net_pnl"],
                "xau_pnl": m_xau["net_pnl"],
                "2023": yr_pnls[2023],
                "2024": yr_pnls[2024],
                "2025": yr_pnls[2025],
                "2026": yr_pnls[2026],
                "+yrs": f"{pos_years}/4",
                "style": note,
            }
        )

    # 1. Existing SMC baselines on calibrated 4-year data
    t_15, _ = run_smc_backtest(raw_by_sym, start, end, ltf="15min")
    record("SMC_15M_BASE", t_15, "15m SMC Limit 2R (current live)")

    t_30, _ = run_smc_backtest(raw_by_sym, start, end, ltf="30min")
    record("SMC_30M_BASE", t_30, "30m SMC Limit 2R")

    t_15_nq, _ = run_smc_backtest({"NQ": raw_by_sym["NQ"]}, start, end, ltf="15min")
    record("SMC_15M_NQ_ONLY", t_15_nq, "15m SMC NQ only")

    t_30_xau, _ = run_smc_backtest({"XAUUSD": raw_by_sym["XAUUSD"]}, start, end, ltf="30min")
    record("SMC_30M_XAU_ONLY", t_30_xau, "30m SMC XAU only")

    # Hybrid 15m NQ + 30m XAU standalone sum/combined check (run on merged signals cleanly)
    t_hyb = sorted([t for t in (t_15_nq + t_30_xau) if t.status == TradeStatus.CLOSED], key=lambda x: x.placed_at)
    record("SMC_HYB_15NQ_30XAU", t_hyb, "15m NQ + 30m XAU combined (uncompounded sum)")

    # 2. Bot-Native Strategies
    specs = [
        ("ORB_30M_STOP_EOD", "15m", sig_orb_30m_stop_eod, True, "09:30-10:00 NY ORB stop + EOD"),
        ("ORB_15M_RETEST_LMT", "15m", sig_orb_15m_retest_limit, True, "09:30-09:45 ORB limit retest"),
        ("ORB_SESS_MATCHED", "15m", sig_orb_session_matched, True, "LDN ORB Gold + NY ORB NQ limit"),
        ("ASIAN_SWEEP_FADE", "15m", sig_asian_sweep_fade, True, "Asian box sweep fade 2R"),
        ("ASIAN_BRK_TREND", "30m", sig_asian_breakout_trend, True, "Asian box 30m trend breakout"),
        ("PDH_PDL_SWEEP_FADE", "30m", sig_pdh_pdl_sweep_fade, True, "Prior Day H/L sweep reversal"),
        ("DONCHIAN_1H_TREND", "1h", sig_donchian_1h_trend, False, "1H Turtle 20-bar + 2.5ATR trail"),
        ("SUPERTREND_1H_ADX", "1h", sig_supertrend_1h_adx, False, "1H SuperTrend + EMA200 + ADX"),
        ("EMA_PULLBACK_30M", "30m", sig_ema_pullback_30m, False, "30m EMA20 pullback + 1H trend"),
        ("MEAN_REV_BB_30M", "30m", sig_mean_rev_bb_30m, False, "30m BB/Z-Score fade (ADX<22)"),
        ("RSI2_TREND_1H", "1h", sig_rsi2_trend_1h, False, "1H Connors RSI(2) snapback"),
    ]
    for name, ltf, fn, opd, note in specs:
        ar_trades = run_arena_strategy(frames_by_sym, ltf, fn, start_date=start, end_date=end, one_per_day=opd)
        record(name, _to_report_trades(ar_trades), note)

    df_res = pd.DataFrame(results).sort_values("net_pnl", ascending=False).reset_index(drop=True)
    return df_res


def main() -> None:
    parser = argparse.ArgumentParser(description="4-Year Strategy Arena (NQ & XAUUSD)")
    parser.add_argument("--data-dir", type=Path, default=Path("data_4y"))
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-12-31")
    parser.add_argument("--out-csv", type=Path, default=Path("arena_4y_results.csv"))
    args = parser.parse_args()

    df = evaluate_suite(args.data_dir, args.start, args.end)
    df.to_csv(args.out_csv, index=False)
    print(f"\n=== 4-YEAR STRATEGY ARENA ({args.start} -> {args.end}, NQ & XAUUSD, Realistic Costs) ===")
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
