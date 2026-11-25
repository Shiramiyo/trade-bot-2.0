"""Read-only MT5 analytics, trade reconstruction, Donchian radar, and state/log readers."""

from __future__ import annotations

import csv
import ctypes
import json
import math
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore[assignment]
import pandas as pd

from download_mt5_data import resolve_mt5_symbol
from market_feed import MT5_LOCK
from live_mt5_bot import (
    MAGIC_NUMBER,
    NY_TZ,
    _closed_1h_bars,
    _mt5_epoch_to_ny,
    _rates_to_ny_df,
    compute_lot_size,
    effective_tick_value,
    is_trading_window,
    prepare_live_donchian_frames,
)
from strategy_arena import compute_atr

ROOT_DIR = Path(__file__).resolve().parent
LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc


def _mt5_epoch_info(epoch_seconds: int, server_offset_hours: int = 7) -> Dict[str, Any]:
    """Convert an MT5 broker-clock timestamp into NY and UTC metadata."""
    ny_ts = _mt5_epoch_to_ny(int(epoch_seconds), server_offset_hours=server_offset_hours)
    utc_ts = ny_ts.tz_convert(timezone.utc)
    local_ts = ny_ts.tz_convert(LOCAL_TZ)
    return {
        "ny_ts": ny_ts,
        "utc_iso": utc_ts.isoformat(),
        "ny_str": ny_ts.strftime("%Y-%m-%d %H:%M %Z"),
        "local_str": local_ts.strftime("%Y-%m-%d %H:%M"),
        "epoch_utc": int(utc_ts.timestamp()),
    }


def _is_pid_running(pid: Optional[int]) -> Optional[bool]:
    """Best-effort check on Windows whether a bot PID is still alive."""
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return None
            return exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def summarize_balance_deals(deals: Any, fallback_balance: float = 0.0) -> Dict[str, float]:
    """Extract initial deposit and cumulative non-trade balance flows from MT5 deals."""
    balance_types = {
        getattr(mt5, "DEAL_TYPE_BALANCE", 2),
        getattr(mt5, "DEAL_TYPE_CREDIT", 3),
        getattr(mt5, "DEAL_TYPE_BONUS", 6),
        getattr(mt5, "DEAL_TYPE_CORRECTION", 5),
        getattr(mt5, "DEAL_TYPE_CHARGE", 4),
    }
    cashflows = [
        d
        for d in (deals or [])
        if int(getattr(d, "position_id", 0) or 0) == 0
        and int(getattr(d, "type", -1)) in balance_types
    ]
    cashflows.sort(key=lambda d: int(getattr(d, "time_msc", 0) or getattr(d, "time", 0) * 1000))
    if not cashflows:
        return {"initial_balance": float(fallback_balance), "net_deposits": float(fallback_balance)}
    initial = float(getattr(cashflows[0], "profit", 0.0) or 0.0)
    total = sum(float(getattr(d, "profit", 0.0) or 0.0) for d in cashflows)
    return {
        "initial_balance": round(initial if initial > 0 else total or fallback_balance, 2),
        "net_deposits": round(total, 2),
    }


def reconstruct_trades_from_deals(
    deals: Any,
    bot_only: bool = False,
    magic_number: int = MAGIC_NUMBER,
    server_offset_hours: int = 7,
) -> List[Dict[str, Any]]:
    """
    Pair MT5 entry/exit deals by `position_id` into closed trade records.
    MT5 broker SL/TP exit deals often carry `magic=0`, so `is_bot` checks all deals
    sharing that `position_id`.
    """
    entry_in = getattr(mt5, "DEAL_ENTRY_IN", 0)
    entry_out_types = {
        getattr(mt5, "DEAL_ENTRY_OUT", 1),
        getattr(mt5, "DEAL_ENTRY_INOUT", 2),
        getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
    }
    buy_type = getattr(mt5, "DEAL_TYPE_BUY", 0)
    sell_type = getattr(mt5, "DEAL_TYPE_SELL", 1)

    grouped: Dict[int, List[Any]] = defaultdict(list)
    for d in deals or []:
        pos_id = int(getattr(d, "position_id", 0) or 0)
        d_type = int(getattr(d, "type", -1))
        if pos_id <= 0 or d_type not in (buy_type, sell_type):
            continue
        grouped[pos_id].append(d)

    closed_trades: List[Dict[str, Any]] = []
    for pos_id, pos_deals in grouped.items():
        pos_deals.sort(key=lambda d: (int(getattr(d, "time_msc", 0) or 0), int(getattr(d, "time", 0) or 0), int(getattr(d, "ticket", 0) or 0)))

        in_deals = [d for d in pos_deals if int(getattr(d, "entry", -1)) == entry_in]
        out_deals = [d for d in pos_deals if int(getattr(d, "entry", -1)) in entry_out_types]
        if not in_deals or not out_deals:
            continue

        in_vol = sum(float(getattr(d, "volume", 0.0) or 0.0) for d in in_deals)
        out_vol = sum(float(getattr(d, "volume", 0.0) or 0.0) for d in out_deals)
        if in_vol <= 0 or out_vol < in_vol - 1e-8:
            # Skip positions that are only partially closed and still running.
            continue

        is_bot = any(int(getattr(d, "magic", 0) or 0) == magic_number for d in pos_deals)
        if bot_only and not is_bot:
            continue

        first_in = in_deals[0]
        last_out = out_deals[-1]
        side = "BUY" if int(getattr(first_in, "type", buy_type)) == buy_type else "SELL"
        entry_price = sum(float(d.price) * float(d.volume) for d in in_deals) / in_vol
        exit_price = sum(float(d.price) * float(d.volume) for d in out_deals) / out_vol

        gross_profit = sum(float(getattr(d, "profit", 0.0) or 0.0) for d in pos_deals)
        commission = sum(float(getattr(d, "commission", 0.0) or 0.0) for d in pos_deals)
        swap = sum(float(getattr(d, "swap", 0.0) or 0.0) for d in pos_deals)
        fee = sum(float(getattr(d, "fee", 0.0) or 0.0) for d in pos_deals)
        net_pnl = gross_profit + commission + swap + fee

        t_in = _mt5_epoch_info(int(getattr(first_in, "time", 0)), server_offset_hours)
        t_out = _mt5_epoch_info(int(getattr(last_out, "time", 0)), server_offset_hours)
        dur_hours = max(0.0, (t_out["epoch_utc"] - t_in["epoch_utc"]) / 3600.0)
        pts = (exit_price - entry_price) if side == "BUY" else (entry_price - exit_price)

        entry_comment = str(getattr(first_in, "comment", "") or "").strip()
        exit_comment = str(getattr(last_out, "comment", "") or "").strip()
        bot_magic = next((int(d.magic) for d in pos_deals if int(getattr(d, "magic", 0) or 0) != 0), 0)

        closed_trades.append(
            {
                "position_id": pos_id,
                "symbol": str(getattr(first_in, "symbol", "") or getattr(last_out, "symbol", "")),
                "direction": side,
                "volume": round(in_vol, 4),
                "entry_price": round(entry_price, 5),
                "exit_price": round(exit_price, 5),
                "points": round(pts, 4),
                "entry_time_utc": t_in["utc_iso"],
                "entry_time_ny": t_in["ny_str"],
                "entry_time_local": t_in["local_str"],
                "entry_epoch": t_in["epoch_utc"],
                "exit_time_utc": t_out["utc_iso"],
                "exit_time_ny": t_out["ny_str"],
                "exit_time_local": t_out["local_str"],
                "exit_epoch": t_out["epoch_utc"],
                "duration_hours": round(dur_hours, 2),
                "gross_profit": round(gross_profit, 2),
                "commission": round(commission, 2),
                "swap": round(swap, 2),
                "fee": round(fee, 2),
                "net_pnl": round(net_pnl, 2),
                "is_bot": is_bot,
                "magic": bot_magic,
                "comment": exit_comment or entry_comment or ("DONCHIAN_1H" if is_bot else "Manual"),
            }
        )

    closed_trades.sort(key=lambda t: t["exit_epoch"], reverse=True)
    return closed_trades


def compute_live_performance(
    closed_trades: List[Dict[str, Any]],
    initial_balance: float,
    current_balance: float,
    current_equity: float,
) -> Dict[str, Any]:
    """Compute win rate, profit factor, max drawdown, equity curve, and symbol splits."""
    chronological = sorted(closed_trades, key=lambda t: t["exit_epoch"])
    total = len(chronological)
    pnls = [float(t["net_pnl"]) for t in chronological]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_pnl = sum(pnls)
    win_rate = (len(wins) / total * 100.0) if total > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.99 if gross_profit > 0 else 0.0)
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = -(gross_loss / len(losses)) if losses else 0.0
    expectancy = (net_pnl / total) if total > 0 else 0.0

    # Walk equity curve forward from starting capital
    start_eq = float(initial_balance if initial_balance > 0 else current_balance - net_pnl)
    eq = start_eq
    peak = eq
    max_dd_usd = 0.0
    max_dd_pct = 0.0

    curve: List[Dict[str, Any]] = [
        {
            "label": "Start",
            "utc_iso": chronological[0]["entry_time_utc"] if chronological else datetime.now(timezone.utc).isoformat(),
            "equity": round(start_eq, 2),
            "pnl": 0.0,
            "symbol": "INIT",
        }
    ]
    for t in chronological:
        eq += float(t["net_pnl"])
        if eq > peak:
            peak = eq
        dd_usd = peak - eq
        dd_pct = (dd_usd / peak * 100.0) if peak > 0 else 0.0
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        curve.append(
            {
                "label": t["exit_time_ny"],
                "utc_iso": t["exit_time_utc"],
                "equity": round(eq, 2),
                "pnl": round(float(t["net_pnl"]), 2),
                "symbol": t["symbol"],
            }
        )

    # Append live floating equity point if different from realized equity
    if abs(current_equity - eq) >= 0.01 and current_equity > 0:
        curve.append(
            {
                "label": "Now (Open Eq.)",
                "utc_iso": datetime.now(timezone.utc).isoformat(),
                "equity": round(current_equity, 2),
                "pnl": round(current_equity - current_balance, 2),
                "symbol": "LIVE",
            }
        )

    # Per-symbol breakdown
    sym_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in chronological:
        sym_groups[t["symbol"]].append(t)

    by_symbol: List[Dict[str, Any]] = []
    for sym, sym_trades in sorted(sym_groups.items()):
        s_pnls = [float(t["net_pnl"]) for t in sym_trades]
        s_wins = [p for p in s_pnls if p > 0]
        s_losses = [p for p in s_pnls if p < 0]
        gp = sum(s_wins)
        gl = abs(sum(s_losses))
        by_symbol.append(
            {
                "symbol": sym,
                "trades": len(sym_trades),
                "wins": len(s_wins),
                "losses": len(s_losses),
                "win_rate_pct": round(len(s_wins) / len(sym_trades) * 100.0, 2),
                "net_pnl": round(sum(s_pnls), 2),
                "profit_factor": round(gp / gl, 2) if gl > 0 else (99.99 if gp > 0 else 0.0),
            }
        )

    return {
        "initial_balance": round(start_eq, 2),
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(min(profit_factor, 99.99), 2),
        "net_pnl": round(net_pnl, 2),
        "return_pct": round((net_pnl / start_eq * 100.0) if start_eq > 0 else 0.0, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "avg_duration_hours": round(sum(t["duration_hours"] for t in chronological) / total, 2) if total > 0 else 0.0,
        "max_drawdown_usd": round(max_dd_usd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "equity_curve": curve,
        "by_symbol": by_symbol,
    }


def compute_symbol_radar(
    symbol_map: Dict[str, str],
    rates_by_sym: Dict[str, Any],
    ticks_by_sym: Dict[str, Any],
    infos_by_sym: Dict[str, Any],
    balance: float,
    risk_pct: float,
    donchian_period: int,
    sl_atr_mult: float,
    trail_atr_mult: float,
    max_cost_ratio: float,
    now_ny: datetime,
    server_offset_hours: int = 7,
) -> List[Dict[str, Any]]:
    """Build live 1H Donchian channel, EMA alignment, spread check, and candle chart data."""
    items: List[Dict[str, Any]] = []
    risk_amount = max(0.0, balance * risk_pct)

    for canonical, mt5_sym in symbol_map.items():
        rates = rates_by_sym.get(mt5_sym)
        if rates is None or len(rates) < 260:
            items.append(
                {
                    "canonical": canonical,
                    "symbol": mt5_sym,
                    "status": "NO_DATA",
                    "bars_count": len(rates) if rates is not None else 0,
                }
            )
            continue

        raw_h1 = _closed_1h_bars(rates, now_ny=now_ny, server_offset_hours=server_offset_hours)
        if len(raw_h1) < 210:
            items.append({"canonical": canonical, "symbol": mt5_sym, "status": "WARMING_UP", "bars_count": len(raw_h1)})
            continue

        df1h, df4h = prepare_live_donchian_frames(raw_h1, donchian_period=donchian_period)
        row = df1h.iloc[-1]
        r4 = df4h.iloc[-1]
        info = infos_by_sym.get(mt5_sym)
        tick = ticks_by_sym.get(mt5_sym)

        digits = int(getattr(info, "digits", 2) or 2)
        atr = float(row["atr14"])
        cl = float(row["close"])
        don_hi = float(row["don_hi"])
        don_lo = float(row["don_lo"])
        ema50 = float(row["ema50"])
        ema200 = float(row["ema200"])
        ema20_4h = float(r4["ema20"])
        ema50_4h = float(r4["ema50"])

        trend_1h = "BULLISH" if ema50 > ema200 else "BEARISH"
        trend_4h = "BULLISH" if ema20_4h > ema50_4h else "BEARISH"
        aligned = trend_1h == trend_4h
        alignment = trend_1h if aligned else "MIXED"

        bid = float(getattr(tick, "bid", cl) or cl)
        ask = float(getattr(tick, "ask", cl) or cl)
        spread = max(0.0, ask - bid)

        sl_dist = sl_atr_mult * atr if atr > 0 else 0.0
        trail_dist = trail_atr_mult * atr if atr > 0 else 0.0

        est_lots = 0.0
        spread_cost = 0.0
        spread_ratio_pct = 0.0
        spread_ok = True
        if info is not None and sl_dist > 0:
            tick_val = effective_tick_value(mt5_sym, info, cl)
            est_lots = compute_lot_size(
                risk_amount=risk_amount,
                sl_distance=sl_dist,
                tick_size=float(info.trade_tick_size),
                tick_value=tick_val,
                vol_min=float(info.volume_min),
                vol_max=float(info.volume_max),
                vol_step=float(info.volume_step),
            )
            if est_lots > 0 and info.trade_tick_size > 0:
                spread_cost = (spread / float(info.trade_tick_size)) * tick_val * est_lots
                spread_ratio_pct = (spread_cost / risk_amount * 100.0) if risk_amount > 0 else 0.0
                spread_ok = spread_cost <= risk_amount * max_cost_ratio

        width = max(1e-9, don_hi - don_lo)
        channel_pct = max(0.0, min(100.0, (cl - don_lo) / width * 100.0))

        recent_slice = df1h.tail(60)
        bars_payload = [
            {
                "t": idx.strftime("%m-%d %H:%M"),
                "o": round(float(b["open"]), digits),
                "h": round(float(b["high"]), digits),
                "l": round(float(b["low"]), digits),
                "c": round(float(b["close"]), digits),
                "don_hi": round(float(b["don_hi"]), digits) if not pd.isna(b["don_hi"]) else None,
                "don_lo": round(float(b["don_lo"]), digits) if not pd.isna(b["don_lo"]) else None,
                "ema50": round(float(b["ema50"]), digits) if not pd.isna(b["ema50"]) else None,
            }
            for idx, b in recent_slice.iterrows()
        ]

        items.append(
            {
                "canonical": canonical,
                "symbol": mt5_sym,
                "status": "READY",
                "digits": digits,
                "last_bar_ny": df1h.index[-1].strftime("%Y-%m-%d %H:%M %Z"),
                "bid": round(bid, digits),
                "ask": round(ask, digits),
                "close": round(cl, digits),
                "don_hi": round(don_hi, digits),
                "don_lo": round(don_lo, digits),
                "channel_pct": round(channel_pct, 1),
                "dist_to_long": round(don_hi - cl, digits),
                "dist_to_short": round(cl - don_lo, digits),
                "ema50_1h": round(ema50, digits),
                "ema200_1h": round(ema200, digits),
                "ema20_4h": round(ema20_4h, digits),
                "ema50_4h": round(ema50_4h, digits),
                "trend_1h": trend_1h,
                "trend_4h": trend_4h,
                "alignment": alignment,
                "atr14": round(atr, digits),
                "sl_dist": round(sl_dist, digits),
                "trail_dist": round(trail_dist, digits),
                "est_lots_1pct": round(est_lots, 2),
                "spread": round(spread, digits),
                "spread_cost_usd": round(spread_cost, 2),
                "spread_ratio_pct": round(spread_ratio_pct, 2),
                "spread_ok": spread_ok,
                "bars": bars_payload,
            }
        )

    return items


def build_chart_payload(
    mt5_sym: str,
    timeframe: str,
    rates_tf: Any,
    rates_h1: Any,
    tick: Any,
    info: Any,
    balance: float,
    risk_pct: float = 0.01,
    donchian_period: int = 20,
    sl_atr_mult: float = 2.0,
    trail_atr_mult: float = 2.5,
    bars_limit: int = 360,
    now_ny: Optional[datetime] = None,
    server_offset_hours: int = 7,
) -> Dict[str, Any]:
    """Build TradingView Lightweight-Charts OHLC, Donchian/EMA overlays, and Long/Short forecast data."""
    if now_ny is None:
        now_ny = datetime.now(timezone.utc).astimezone(NY_TZ)

    digits = int(getattr(info, "digits", 2) or 2)
    candles: List[Dict[str, Any]] = []
    don_hi_pts: List[Dict[str, Any]] = []
    don_lo_pts: List[Dict[str, Any]] = []
    ema50_pts: List[Dict[str, Any]] = []
    ema200_pts: List[Dict[str, Any]] = []

    if rates_tf is not None and len(rates_tf) > 0:
        df_tf = _rates_to_ny_df(rates_tf, server_offset_hours=server_offset_hours)
        df_tf["ema50"] = df_tf["close"].ewm(span=50, adjust=False).mean()
        df_tf["ema200"] = df_tf["close"].ewm(span=200, adjust=False).mean()
        df_tf["don_hi"] = df_tf["high"].rolling(donchian_period).max().shift(1)
        df_tf["don_lo"] = df_tf["low"].rolling(donchian_period).min().shift(1)
        df_tf["atr14"] = compute_atr(df_tf, 14)

        tail_tf = df_tf.tail(max(30, bars_limit))
        for idx, row in tail_tf.iterrows():
            ts = int(idx.tz_convert(timezone.utc).timestamp())
            candles.append(
                {
                    "time": ts,
                    "open": round(float(row["open"]), digits),
                    "high": round(float(row["high"]), digits),
                    "low": round(float(row["low"]), digits),
                    "close": round(float(row["close"]), digits),
                }
            )
            if not pd.isna(row["don_hi"]):
                don_hi_pts.append({"time": ts, "value": round(float(row["don_hi"]), digits)})
            if not pd.isna(row["don_lo"]):
                don_lo_pts.append({"time": ts, "value": round(float(row["don_lo"]), digits)})
            if not pd.isna(row["ema50"]):
                ema50_pts.append({"time": ts, "value": round(float(row["ema50"]), digits)})
            if not pd.isna(row["ema200"]):
                ema200_pts.append({"time": ts, "value": round(float(row["ema200"]), digits)})

    # Always compute canonical 1H Donchian Trend Forecast levels from closed 1H bars
    forecast: Optional[Dict[str, Any]] = None
    if rates_h1 is not None and len(rates_h1) >= 250:
        closed_h1 = _closed_1h_bars(rates_h1, now_ny=now_ny, server_offset_hours=server_offset_hours)
        if len(closed_h1) >= 210:
            df1h, df4h = prepare_live_donchian_frames(closed_h1, donchian_period=donchian_period)
            last_h1 = df1h.iloc[-1]
            last_h4 = df4h.iloc[-1]
            atr14 = float(last_h1["atr14"])
            don_hi = float(last_h1["don_hi"])
            don_lo = float(last_h1["don_lo"])
            sl_dist = sl_atr_mult * atr14 if atr14 > 0 else 0.0
            trail_dist = trail_atr_mult * atr14 if atr14 > 0 else 0.0
            risk_usd = round(max(0.0, balance * risk_pct), 2)

            est_lots = 0.0
            if info is not None and sl_dist > 0:
                tick_val = effective_tick_value(mt5_sym, info, float(last_h1["close"]))
                est_lots = compute_lot_size(
                    risk_amount=risk_usd,
                    sl_distance=sl_dist,
                    tick_size=float(info.trade_tick_size),
                    tick_value=tick_val,
                    vol_min=float(info.volume_min),
                    vol_max=float(info.volume_max),
                    vol_step=float(info.volume_step),
                )

            trend_1h = "BULLISH" if float(last_h1["ema50"]) > float(last_h1["ema200"]) else "BEARISH"
            trend_4h = "BULLISH" if float(last_h4["ema20"]) > float(last_h4["ema50"]) else "BEARISH"
            recommended = trend_1h if trend_1h == trend_4h else "MIXED"

            if sl_dist > 0 and not pd.isna(don_hi) and not pd.isna(don_lo):
                long_entry = round(don_hi, digits)
                short_entry = round(don_lo, digits)
                forecast = {
                    "bar_epoch": int(df1h.index[-1].tz_convert(timezone.utc).timestamp()),
                    "bar_ny": df1h.index[-1].strftime("%Y-%m-%d %H:%M %Z"),
                    "trend_1h": trend_1h,
                    "trend_4h": trend_4h,
                    "recommended": recommended,
                    "atr14": round(atr14, digits),
                    "sl_dist": round(sl_dist, digits),
                    "trail_dist": round(trail_dist, digits),
                    "risk_usd": risk_usd,
                    "est_lots": round(est_lots, 2),
                    "long": {
                        "side": "BUY",
                        "entry": long_entry,
                        "sl": round(long_entry - sl_dist, digits),
                        "tp_1r": round(long_entry + sl_dist, digits),
                        "tp_2r": round(long_entry + 2.0 * sl_dist, digits),
                        "tp_3r": round(long_entry + 3.0 * sl_dist, digits),
                    },
                    "short": {
                        "side": "SELL",
                        "entry": short_entry,
                        "sl": round(short_entry + sl_dist, digits),
                        "tp_1r": round(short_entry - sl_dist, digits),
                        "tp_2r": round(short_entry - 2.0 * sl_dist, digits),
                        "tp_3r": round(short_entry - 3.0 * sl_dist, digits),
                    },
                }

    bid = float(getattr(tick, "bid", candles[-1]["close"] if candles else 0.0) or 0.0)
    ask = float(getattr(tick, "ask", bid) or bid)

    return {
        "symbol": mt5_sym,
        "timeframe": timeframe,
        "digits": digits,
        "bid": round(bid, digits),
        "ask": round(ask, digits),
        "candles": candles,
        "indicators": {
            "don_hi": don_hi_pts,
            "don_lo": don_lo_pts,
            "ema50": ema50_pts,
            "ema200": ema200_pts,
        },
        "forecast": forecast,
    }


def load_arena_reports(csv_path: Path | str = ROOT_DIR / "arena_4y_results.csv") -> List[Dict[str, Any]]:
    """Load the 4-year strategy comparison results CSV."""
    path = Path(csv_path)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(dict(row))
    return rows


def tail_bot_log(log_path: Path | str = ROOT_DIR / "live_bot.log", limit: int = 150) -> List[Dict[str, str]]:
    """Read the last `limit` lines of `live_bot.log` safely without loading huge files."""
    path = Path(log_path)
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        read_bytes = min(size, 262_144)
        with path.open("rb") as fh:
            if size > read_bytes:
                fh.seek(size - read_bytes)
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    lines = raw.splitlines()[-limit:]
    parsed: List[Dict[str, str]] = []
    for line in reversed(lines):
        level = "INFO"
        ts = ""
        msg = line
        if "] " in line and "[" in line:
            left, rest = line.split("] ", 1)
            if " [" in left:
                ts, level = left.rsplit(" [", 1)
                msg = rest
        parsed.append({"ts": ts.strip(), "level": level.strip(), "msg": msg.strip(), "raw": line})
    return parsed


def read_bot_heartbeat(state_path: Path | str = ROOT_DIR / "live_state.json") -> Dict[str, Any]:
    """Read `live_state.json` and assess whether `live_mt5_bot.py` is currently active."""
    path = Path(state_path)
    if not path.exists():
        return {"present": False, "alive": False, "age_seconds": None, "state": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": False, "alive": False, "age_seconds": None, "state": None}

    updated_raw = data.get("updated_at_utc")
    age_s: Optional[float] = None
    if updated_raw:
        try:
            updated_dt = datetime.fromisoformat(updated_raw)
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age_s = max(0.0, (datetime.now(timezone.utc) - updated_dt).total_seconds())
        except ValueError:
            age_s = None

    poll_s = int(data.get("poll_seconds", 30) or 30)
    pid = data.get("pid")
    pid_alive = _is_pid_running(pid)
    fresh = age_s is not None and age_s <= max(90.0, poll_s * 3.5)
    alive = fresh and (pid_alive is not False)
    return {
        "present": True,
        "alive": alive,
        "pid": pid,
        "pid_alive": pid_alive,
        "age_seconds": round(age_s, 1) if age_s is not None else None,
        "state": data,
    }


class TerminalDataService:
    """Thread-safe, TTL-cached read-only snapshot service for the Web Trading Terminal."""

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        history_days: int = 180,
        server_offset_hours: int = 7,
        cache_ttl_seconds: float = 5.0,
        state_path: Path | str = ROOT_DIR / "live_state.json",
        log_path: Path | str = ROOT_DIR / "live_bot.log",
        reports_path: Path | str = ROOT_DIR / "arena_4y_results.csv",
        public_showcase: bool = False,
    ) -> None:
        self.symbols = symbols or ["NQ", "XAUUSD"]
        self.history_days = history_days
        self.server_offset_hours = server_offset_hours
        self.cache_ttl_seconds = cache_ttl_seconds
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        self.reports_path = Path(reports_path)
        self.public_showcase = public_showcase
        self._lock = MT5_LOCK
        self._mt5_ready = False
        self._cache: Dict[str, Any] = {}
        self._cache_ts: float = 0.0

    def _ensure_mt5(self) -> bool:
        if mt5 is None:
            return False
        if self._mt5_ready and mt5.account_info() is not None:
            return True
        self._mt5_ready = bool(mt5.initialize())
        return self._mt5_ready

    def get_snapshot(self, scope: str = "all", force: bool = False) -> Dict[str, Any]:
        """Return full dashboard JSON payload for `scope` ('bot' or 'all')."""
        bot_only = scope == "bot"
        with self._lock:
            now_mono = time.monotonic()
            if not force and self._cache and (now_mono - self._cache_ts) < self.cache_ttl_seconds:
                base = self._cache
            else:
                base = self._collect_base_snapshot()
                self._cache = base
                self._cache_ts = now_mono

        deals = base["_deals"]
        initial_bal = base["_balance_summary"]["initial_balance"]
        acct = base["account"]
        closed_trades = reconstruct_trades_from_deals(
            deals,
            bot_only=bot_only,
            magic_number=MAGIC_NUMBER,
            server_offset_hours=self.server_offset_hours,
        )
        performance = compute_live_performance(
            closed_trades=closed_trades,
            initial_balance=initial_bal,
            current_balance=float(acct.get("balance", initial_bal)),
            current_equity=float(acct.get("equity", initial_bal)),
        )

        raw_positions = base["positions"]
        positions = [p for p in raw_positions if (not bot_only or p["is_bot"])]

        return {
            "scope": "bot" if bot_only else "all",
            "generated_at_utc": base["generated_at_utc"],
            "generated_at_ny": base["generated_at_ny"],
            "generated_at_local": base["generated_at_local"],
            "mt5_connected": base["mt5_connected"],
            "mt5_error": base["mt5_error"],
            "in_window": base["in_window"],
            "public_showcase": bool(base.get("public_showcase", self.public_showcase)),
            "bot": base["bot"],
            "account": acct,
            "positions": positions,
            "trades": closed_trades[:300],
            "total_trades_count": len(closed_trades),
            "performance": performance,
            "radar": base["radar"],
        }

    def _collect_base_snapshot(self) -> Dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        now_ny = now_utc.astimezone(NY_TZ)
        now_local = now_utc.astimezone(LOCAL_TZ)
        in_window = is_trading_window(now_ny)

        if self.public_showcase:
            return {
                "generated_at_utc": now_utc.isoformat(),
                "generated_at_ny": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "generated_at_local": now_local.strftime("%Y-%m-%d %H:%M:%S"),
                "mt5_connected": False,
                "mt5_error": "Public Showcase Mode — Live TradingView Chart Studio & 4-Year Strategy Arena are active. Private MT5 telemetry is hidden.",
                "in_window": in_window,
                "public_showcase": True,
                "bot": {
                    "alive": False,
                    "present": False,
                    "pid": None,
                    "age_seconds": None,
                    "strategy": "DONCHIAN_1H(lb=20, sl=2.0ATR, trail=2.5ATR)",
                    "dry_run": None,
                    "dd_blocked": False,
                    "daily_dd_pct": 0.0,
                    "max_daily_dd_pct": 3.0,
                    "risk_pct": 1.0,
                    "updated_at_ny": None,
                    "magic_number": MAGIC_NUMBER,
                },
                "account": {
                    "login": None,
                    "server": "Public Showcase",
                    "currency": "USD",
                    "mode": "SHOWCASE",
                    "balance": 0.0,
                    "equity": 0.0,
                    "margin": 0.0,
                    "margin_free": 0.0,
                    "floating_pnl": 0.0,
                    "leverage": 0,
                    "algo_allowed": None,
                },
                "positions": [],
                "radar": [],
                "_deals": [],
                "_balance_summary": {"initial_balance": 0.0, "net_deposits": 0.0},
            }

        heartbeat = read_bot_heartbeat(self.state_path)
        hb_state = heartbeat.get("state") or {}

        risk_pct = float(hb_state.get("risk_pct", 0.01) or 0.01)
        don_period = int(hb_state.get("donchian_period", 20) or 20)
        sl_atr = float(hb_state.get("sl_atr_mult", 2.0) or 2.0)
        trail_atr = float(hb_state.get("trail_atr_mult", 2.5) or 2.5)
        max_cost = float(hb_state.get("max_cost_ratio", 0.20) or 0.20)

        mt5_ok = self._ensure_mt5()
        mt5_err_detail = "Python MetaTrader5 package not installed" if mt5 is None else mt5.last_error()
        mt5_error = None if mt5_ok else f"MT5 not connected ({mt5_err_detail})"

        acct_payload: Dict[str, Any] = {
            "login": hb_state.get("account_login"),
            "server": "MetaQuotes-Demo",
            "currency": "USD",
            "mode": hb_state.get("mode", "DEMO"),
            "balance": float(hb_state.get("account_balance", 0.0) or 0.0),
            "equity": float(hb_state.get("account_equity", 0.0) or 0.0),
            "margin": 0.0,
            "margin_free": 0.0,
            "floating_pnl": 0.0,
            "leverage": 0,
            "algo_allowed": None,
        }
        positions_payload: List[Dict[str, Any]] = []
        deals_list: List[Any] = []
        radar_payload: List[Dict[str, Any]] = []
        balance_summary = {"initial_balance": acct_payload["balance"], "net_deposits": acct_payload["balance"]}

        if mt5_ok:
            acct = mt5.account_info()
            term = mt5.terminal_info()
            if acct is not None:
                mode_label = "DEMO" if acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL"
                acct_payload = {
                    "login": int(acct.login),
                    "server": str(acct.server),
                    "currency": str(acct.currency),
                    "mode": mode_label,
                    "balance": round(float(acct.balance), 2),
                    "equity": round(float(acct.equity), 2),
                    "margin": round(float(acct.margin), 2),
                    "margin_free": round(float(acct.margin_free), 2),
                    "floating_pnl": round(float(acct.profit), 2),
                    "leverage": int(acct.leverage),
                    "algo_allowed": bool(getattr(term, "trade_allowed", False) and getattr(acct, "trade_allowed", False))
                    if term is not None
                    else None,
                }

            raw_positions = mt5.positions_get() or []
            buy_pos = getattr(mt5, "POSITION_TYPE_BUY", 0)
            for p in raw_positions:
                p_info = mt5.symbol_info(p.symbol)
                digits = int(getattr(p_info, "digits", 2) or 2)
                t_open = _mt5_epoch_info(int(p.time), self.server_offset_hours)
                positions_payload.append(
                    {
                        "ticket": int(p.ticket),
                        "symbol": str(p.symbol),
                        "direction": "BUY" if int(p.type) == buy_pos else "SELL",
                        "volume": round(float(p.volume), 4),
                        "price_open": round(float(p.price_open), digits),
                        "price_current": round(float(p.price_current), digits),
                        "sl": round(float(p.sl), digits),
                        "tp": round(float(p.tp), digits),
                        "profit": round(float(p.profit), 2),
                        "swap": round(float(p.swap), 2),
                        "magic": int(p.magic),
                        "is_bot": int(p.magic) == MAGIC_NUMBER,
                        "comment": str(getattr(p, "comment", "") or ""),
                        "time_ny": t_open["ny_str"],
                        "time_local": t_open["local_str"],
                        "open_epoch": t_open["epoch_utc"],
                        "digits": digits,
                    }
                )

            from_dt = now_utc - timedelta(days=self.history_days)
            to_dt = now_utc + timedelta(days=2)
            deals_list = list(mt5.history_deals_get(from_dt, to_dt) or [])
            balance_summary = summarize_balance_deals(deals_list, fallback_balance=acct_payload["balance"])

            symbol_map: Dict[str, str] = {}
            for sym in self.symbols:
                resolved = resolve_mt5_symbol(sym)
                if resolved:
                    symbol_map[sym] = resolved
            if hb_state.get("symbols"):
                symbol_map = dict(hb_state["symbols"])

            rates_by_sym: Dict[str, Any] = {}
            ticks_by_sym: Dict[str, Any] = {}
            infos_by_sym: Dict[str, Any] = {}
            for _, mt5_sym in symbol_map.items():
                rates_by_sym[mt5_sym] = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_H1, 0, 500)
                ticks_by_sym[mt5_sym] = mt5.symbol_info_tick(mt5_sym)
                infos_by_sym[mt5_sym] = mt5.symbol_info(mt5_sym)

            radar_payload = compute_symbol_radar(
                symbol_map=symbol_map,
                rates_by_sym=rates_by_sym,
                ticks_by_sym=ticks_by_sym,
                infos_by_sym=infos_by_sym,
                balance=float(acct_payload["balance"]),
                risk_pct=risk_pct,
                donchian_period=don_period,
                sl_atr_mult=sl_atr,
                trail_atr_mult=trail_atr,
                max_cost_ratio=max_cost,
                now_ny=now_ny,
                server_offset_hours=self.server_offset_hours,
            )

        return {
            "generated_at_utc": now_utc.isoformat(),
            "generated_at_ny": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "generated_at_local": now_local.strftime("%Y-%m-%d %H:%M:%S"),
            "mt5_connected": mt5_ok,
            "mt5_error": mt5_error,
            "in_window": in_window,
            "bot": {
                "alive": heartbeat["alive"],
                "present": heartbeat["present"],
                "pid": heartbeat.get("pid"),
                "age_seconds": heartbeat.get("age_seconds"),
                "strategy": hb_state.get("strategy", f"DONCHIAN_1H(lb={don_period}, sl={sl_atr:.1f}ATR, trail={trail_atr:.1f}ATR)"),
                "dry_run": hb_state.get("dry_run"),
                "dd_blocked": hb_state.get("dd_blocked", False),
                "daily_dd_pct": hb_state.get("daily_dd_pct", 0.0),
                "max_daily_dd_pct": round(float(hb_state.get("max_daily_dd", 0.03) or 0.03) * 100.0, 1),
                "risk_pct": round(risk_pct * 100.0, 2),
                "updated_at_ny": hb_state.get("updated_at_ny"),
                "magic_number": MAGIC_NUMBER,
            },
            "account": acct_payload,
            "positions": positions_payload,
            "radar": radar_payload,
            "_deals": deals_list,
            "_balance_summary": balance_summary,
        }

    def get_logs(self, limit: int = 150) -> List[Dict[str, str]]:
        if self.public_showcase:
            msg = "Private bot logs are hidden in Public Showcase mode. Open the Chart or Reports tab for live feeds and 4Y backtests."
            return [{"ts": "", "level": "INFO", "msg": msg, "raw": f"[INFO] {msg}"}]
        return tail_bot_log(self.log_path, limit=limit)

    def get_reports(self) -> List[Dict[str, Any]]:
        return load_arena_reports(self.reports_path)

    def get_chart(self, symbol: str = "USTEC", timeframe: str = "1h", bars: int = 360) -> Dict[str, Any]:
        """Return OHLC candles, Donchian/EMA overlays, and Long/Short Forecast levels."""
        tf_map = {
            "15m": getattr(mt5, "TIMEFRAME_M15", 15),
            "1h": getattr(mt5, "TIMEFRAME_H1", 16385),
            "4h": getattr(mt5, "TIMEFRAME_H4", 16388),
        }
        tf_key = timeframe.lower()
        if tf_key not in tf_map:
            tf_key = "1h"

        if self.public_showcase:
            return {
                "symbol": symbol,
                "timeframe": tf_key,
                "digits": 2,
                "candles": [],
                "indicators": {"don_hi": [], "don_lo": [], "ema50": [], "ema200": []},
                "forecast": None,
                "error": "Legacy MT5 chart endpoint is disabled in Public Showcase mode; use /api/tv/chart.",
            }

        hb = read_bot_heartbeat(self.state_path)
        hb_state = hb.get("state") or {}
        risk_pct = float(hb_state.get("risk_pct", 0.01) or 0.01)
        don_period = int(hb_state.get("donchian_period", 20) or 20)
        sl_atr = float(hb_state.get("sl_atr_mult", 2.0) or 2.0)
        trail_atr = float(hb_state.get("trail_atr_mult", 2.5) or 2.5)

        with self._lock:
            mt5_ok = self._ensure_mt5()
            mt5_sym = symbol
            if mt5_ok and mt5.symbol_info(symbol) is None:
                resolved = resolve_mt5_symbol(symbol)
                if resolved:
                    mt5_sym = resolved

            if not mt5_ok:
                mt5_err_detail = "Python MetaTrader5 package not installed" if mt5 is None else mt5.last_error()
                return {
                    "symbol": mt5_sym,
                    "timeframe": tf_key,
                    "digits": 2,
                    "candles": [],
                    "indicators": {"don_hi": [], "don_lo": [], "ema50": [], "ema200": []},
                    "forecast": None,
                    "error": f"MT5 not connected ({mt5_err_detail})",
                }

            acct = mt5.account_info()
            balance = float(getattr(acct, "balance", 100000.0) or 100000.0)
            fetch_n = max(480, bars + 220)
            rates_tf = mt5.copy_rates_from_pos(mt5_sym, tf_map[tf_key], 0, fetch_n)
            rates_h1 = (
                rates_tf
                if tf_key == "1h" and rates_tf is not None and len(rates_tf) >= 450
                else mt5.copy_rates_from_pos(mt5_sym, tf_map["1h"], 0, 500)
            )
            tick = mt5.symbol_info_tick(mt5_sym)
            info = mt5.symbol_info(mt5_sym)

        now_ny = datetime.now(timezone.utc).astimezone(NY_TZ)
        return build_chart_payload(
            mt5_sym=mt5_sym,
            timeframe=tf_key,
            rates_tf=rates_tf,
            rates_h1=rates_h1,
            tick=tick,
            info=info,
            balance=balance,
            risk_pct=risk_pct,
            donchian_period=don_period,
            sl_atr_mult=sl_atr,
            trail_atr_mult=trail_atr,
            bars_limit=bars,
            now_ny=now_ny,
            server_offset_hours=self.server_offset_hours,
        )
