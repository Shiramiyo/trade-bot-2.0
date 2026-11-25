"""Live/Demo MT5 execution runner for the NQ + XAUUSD 1H Donchian Trend strategy."""

import argparse
import json
import logging
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore[assignment]
import pandas as pd

from download_mt5_data import resolve_mt5_symbol
from models import Bias
from strategy_arena import AGG, _last_closed_htf_idx, compute_atr

NY_TZ = ZoneInfo("America/New_York")
MAGIC_NUMBER = 20261114

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("live_bot.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("live_mt5_bot")


def is_trading_window(ts_ny: datetime, year_end_blackout_days: int = 5) -> bool:
    """Return True Mon-Fri (until Fri 16:00 NY close), excluding Dec 27-31 blackout."""
    if ts_ny.weekday() >= 5:
        return False
    if ts_ny.weekday() == 4 and ts_ny.hour >= 16:
        return False
    if (date(ts_ny.year, 12, 31) - ts_ny.date()).days < year_end_blackout_days:
        return False
    return True


def compute_lot_size(
    risk_amount: float,
    sl_distance: float,
    tick_size: float,
    tick_value: float,
    vol_min: float,
    vol_max: float,
    vol_step: float,
) -> float:
    """Calculate MT5 lot volume floored to `vol_step` so max loss <= `risk_amount`."""
    if risk_amount <= 0 or sl_distance <= 0 or tick_size <= 0 or tick_value <= 0 or vol_step <= 0:
        return 0.0
    loss_per_lot = (sl_distance / tick_size) * tick_value
    if loss_per_lot <= 0:
        return 0.0
    raw_lots = risk_amount / loss_per_lot
    steps = math.floor(raw_lots / vol_step)
    lots = round(steps * vol_step, 8)
    if lots < vol_min:
        return 0.0
    return min(lots, vol_max)


def effective_tick_value(mt5_sym: str, info, ref_price: float = 0.0) -> float:
    """
    Return true dollars-per-tick for 1.0 lot using `mt5.order_calc_profit` when available.
    Protects against brokers whose `trade_tick_value` metadata is off by 10x (e.g. XAUUSD).
    """
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    tick_value = float(
        getattr(info, "trade_tick_value_loss", 0.0)
        or getattr(info, "trade_tick_value", 0.0)
        or 0.0
    )
    if tick_size <= 0 or ref_price <= 0 or not mt5_sym:
        return tick_value

    calc_fn = getattr(mt5, "order_calc_profit", None)
    if callable(calc_fn):
        try:
            buy_type = getattr(mt5, "ORDER_TYPE_BUY", 0)
            profit_100 = calc_fn(
                buy_type,
                mt5_sym,
                1.0,
                float(ref_price),
                float(ref_price) + 100.0 * tick_size,
            )
            if profit_100 is not None and float(profit_100) > 0:
                return abs(float(profit_100)) / 100.0
        except Exception:
            pass
    return tick_value


def _mt5_epoch_to_ny(epoch_seconds: int, server_offset_hours: int = 7) -> pd.Timestamp:
    """Convert an MT5 broker-clock timestamp (e.g. MetaQuotes-Demo NY+7h) to `NY_TZ`."""
    ny_naive = pd.to_datetime(int(epoch_seconds), unit="s") - pd.Timedelta(hours=server_offset_hours)
    return ny_naive.tz_localize(NY_TZ, ambiguous=False, nonexistent="shift_forward")


def _rates_to_ny_df(rates, server_offset_hours: int = 7) -> pd.DataFrame:
    """Convert MT5 rate records into an OHLC DataFrame indexed in `America/New_York`."""
    df = pd.DataFrame(rates)
    ny_naive = pd.to_datetime(df["time"].astype("int64"), unit="s") - pd.Timedelta(hours=server_offset_hours)
    ny_aware = ny_naive.dt.tz_localize(NY_TZ, ambiguous="NaT", nonexistent="shift_forward")
    df = df.assign(timestamp=ny_aware).dropna(subset=["timestamp"])
    df = df.set_index("timestamp")[["open", "high", "low", "close"]].astype(float).sort_index()
    return df[~df.index.duplicated(keep="last")]


def _closed_1h_bars(rates, now_ny: datetime, server_offset_hours: int = 7) -> pd.DataFrame:
    """Return strictly closed 1H bars (`bar_open + 1h <= now_ny`)."""
    df = _rates_to_ny_df(rates, server_offset_hours=server_offset_hours)
    return df[df.index + timedelta(hours=1) <= pd.Timestamp(now_ny)]


def prepare_live_donchian_frames(
    df1h: pd.DataFrame,
    donchian_period: int = 20,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Compute closed-bar 1H and 4H indicators matching `DONCHIAN_1H_TREND`."""
    h1 = df1h.copy()
    h1["ema50"] = h1["close"].ewm(span=50, adjust=False).mean()
    h1["ema200"] = h1["close"].ewm(span=200, adjust=False).mean()
    h1["atr14"] = compute_atr(h1, 14)
    h1["don_hi"] = h1["high"].rolling(donchian_period).max().shift(1)
    h1["don_lo"] = h1["low"].rolling(donchian_period).min().shift(1)

    h4 = h1[["open", "high", "low", "close"]].resample("4h").agg(AGG).dropna()
    h4["ema20"] = h4["close"].ewm(span=20, adjust=False).mean()
    h4["ema50"] = h4["close"].ewm(span=50, adjust=False).mean()
    return h1, h4


def evaluate_donchian_1h_signal(
    df1h: pd.DataFrame,
    df4h: pd.DataFrame,
    sl_atr_mult: float = 2.0,
) -> Optional[Tuple[Bias, float, float, float]]:
    """
    Evaluate `DONCHIAN_1H_TREND` on the latest closed 1H bar (`df1h.iloc[-1]`).
    Returns `(direction, ref_close, sl_price, atr14)` or `None`.
    """
    if len(df1h) < 206 or len(df4h) < 51:
        return None

    row = df1h.iloc[-1]
    prev = df1h.iloc[-2]
    now_close = df1h.index[-1] + timedelta(hours=1)
    i4 = _last_closed_htf_idx(df4h, now_close, timedelta(hours=4))
    if i4 < 50:
        return None
    r4 = df4h.iloc[i4]

    atr = float(row["atr14"])
    cl = float(row["close"])
    hi = float(row["don_hi"])
    lo = float(row["don_lo"])
    if pd.isna(hi) or pd.isna(lo) or pd.isna(prev["don_hi"]) or pd.isna(prev["don_lo"]) or atr <= 0:
        return None

    if cl > hi and float(prev["close"]) <= float(prev["don_hi"]) and row["ema50"] > row["ema200"] and r4["ema20"] > r4["ema50"]:
        return Bias.BULLISH, cl, cl - sl_atr_mult * atr, atr
    if cl < lo and float(prev["close"]) >= float(prev["don_lo"]) and row["ema50"] < row["ema200"] and r4["ema20"] < r4["ema50"]:
        return Bias.BEARISH, cl, cl + sl_atr_mult * atr, atr
    return None


def _pick_filling_mode(info) -> int:
    """Choose a supported MT5 order filling mode from symbol bitmask (IOC -> FOK -> RETURN)."""
    mode = int(getattr(info, "filling_mode", 0) or 0)
    if mode & 2:  # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
    if mode & 1:  # SYMBOL_FILLING_FOK
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _get_bot_positions(mt5_sym: str) -> list:
    """Return open MT5 positions for `mt5_sym` owned by this bot (`MAGIC_NUMBER`)."""
    positions = mt5.positions_get(symbol=mt5_sym) or []
    return [p for p in positions if p.magic == MAGIC_NUMBER]


def _place_market_order(
    mt5_sym: str,
    direction: Bias,
    sl_distance: float,
    risk_amount: float,
    max_cost_ratio: float,
    dry_run: bool,
    ref_price_override: Optional[float] = None,
) -> None:
    """Size and send a market order (or log in dry-run) with initial ATR stop loss."""
    info = mt5.symbol_info(mt5_sym)
    tick = mt5.symbol_info_tick(mt5_sym)
    if info is None or tick is None:
        log.warning("Missing symbol/tick info for %s", mt5_sym)
        return

    digits = int(info.digits)
    if ref_price_override is not None:
        entry_price = round(ref_price_override, digits)
    else:
        entry_price = round(tick.ask if direction == Bias.BULLISH else tick.bid, digits)

    if direction == Bias.BULLISH:
        order_type = mt5.ORDER_TYPE_BUY
        sl_price = round(entry_price - sl_distance, digits)
    else:
        order_type = mt5.ORDER_TYPE_SELL
        sl_price = round(entry_price + sl_distance, digits)

    actual_sl_dist = abs(entry_price - sl_price)
    tick_value = effective_tick_value(mt5_sym, info, entry_price)
    lots = compute_lot_size(
        risk_amount=risk_amount,
        sl_distance=actual_sl_dist,
        tick_size=info.trade_tick_size,
        tick_value=tick_value,
        vol_min=info.volume_min,
        vol_max=info.volume_max,
        vol_step=info.volume_step,
    )
    if lots <= 0:
        log.info("[%s] Signal skipped: computed lot size < volume_min (%s)", mt5_sym, info.volume_min)
        return

    calc_fn = getattr(mt5, "order_calc_profit", None)
    if callable(calc_fn):
        try:
            calc_sl_pnl = calc_fn(order_type, mt5_sym, lots, entry_price, sl_price)
            if calc_sl_pnl is not None and abs(float(calc_sl_pnl)) > risk_amount * 1.15:
                log.warning(
                    "[%s] Lot guard blocked oversized order: lots=%.2f projected SL loss=$%.2f > risk=$%.2f",
                    mt5_sym,
                    lots,
                    abs(float(calc_sl_pnl)),
                    risk_amount,
                )
                return
        except Exception:
            pass

    spread_price = max(0.0, tick.ask - tick.bid)
    spread_cost = (spread_price / info.trade_tick_size) * tick_value * lots
    if spread_cost > risk_amount * max_cost_ratio:
        log.info(
            "[%s] Signal skipped by live spread guard: spread_cost=$%.2f > %.0f%% of $%.2f risk",
            mt5_sym,
            spread_cost,
            max_cost_ratio * 100,
            risk_amount,
        )
        return

    if dry_run:
        log.info(
            "[DRY-RUN] %s %s lots=%.2f entry=%.*f sl=%.*f (2.5ATR trail, no TP cap) spread_cost=$%.2f",
            mt5_sym,
            direction.value,
            lots,
            digits,
            entry_price,
            digits,
            sl_price,
            spread_cost,
        )
        return

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_sym,
        "volume": lots,
        "type": order_type,
        "price": entry_price,
        "sl": sl_price,
        "tp": 0.0,
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": "DONCHIAN_1H",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling_mode(info),
    }
    result = mt5.order_send(req)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("[%s] Order failed: %s (last_error=%s)", mt5_sym, result, mt5.last_error())
    else:
        log.info(
            "[DEMO/LIVE ORDER FILLED] %s %s ticket=%s lots=%.2f price=%.*f sl=%.*f",
            mt5_sym,
            direction.value,
            result.order,
            lots,
            digits,
            getattr(result, "price", entry_price) or entry_price,
            digits,
            sl_price,
        )


def _trail_open_positions(
    mt5_sym: str,
    df1h: pd.DataFrame,
    trail_atr_mult: float,
    server_offset_hours: int,
    dry_run: bool,
    last_trailed_bar: Dict[int, pd.Timestamp],
) -> None:
    """Ratchet SL to `close -+ trail_atr_mult * ATR(14)` once per new closed 1H bar after entry."""
    positions = _get_bot_positions(mt5_sym)
    if not positions:
        return
    info = mt5.symbol_info(mt5_sym)
    tick = mt5.symbol_info_tick(mt5_sym)
    if info is None or tick is None:
        return

    bar_ts = df1h.index[-1]
    bar_close = bar_ts + timedelta(hours=1)
    cl = float(df1h.iloc[-1]["close"])
    atr = float(df1h.iloc[-1]["atr14"])
    if atr <= 0 or pd.isna(atr):
        return

    digits = int(info.digits)
    min_stop_dist = info.trade_stops_level * info.point
    pos_type_buy = getattr(mt5, "POSITION_TYPE_BUY", 0)

    for pos in positions:
        if last_trailed_bar.get(pos.ticket) == bar_ts:
            continue
        pos_open_ny = _mt5_epoch_to_ny(pos.time, server_offset_hours)
        # Only trail on 1H bars that closed strictly after the position was opened.
        if (bar_close - pos_open_ny) <= timedelta(seconds=30):
            continue

        current_sl = float(getattr(pos, "sl", 0.0) or 0.0)

        if pos.type == pos_type_buy:
            candidate_sl = round(cl - trail_atr_mult * atr, digits)
            if candidate_sl <= current_sl + info.point * 0.5:
                last_trailed_bar[pos.ticket] = bar_ts
                continue
            if candidate_sl >= tick.bid - min_stop_dist:
                log.info("[%s] Ticket %s trail SL %.5f too close/above bid %.5f; waiting", mt5_sym, pos.ticket, candidate_sl, tick.bid)
                continue
        else:
            candidate_sl = round(cl + trail_atr_mult * atr, digits)
            if current_sl > 0 and candidate_sl >= current_sl - info.point * 0.5:
                last_trailed_bar[pos.ticket] = bar_ts
                continue
            if candidate_sl <= tick.ask + min_stop_dist:
                log.info("[%s] Ticket %s trail SL %.5f too close/below ask %.5f; waiting", mt5_sym, pos.ticket, candidate_sl, tick.ask)
                continue

        if dry_run:
            last_trailed_bar[pos.ticket] = bar_ts
            log.info(
                "[DRY-RUN TRAIL] %s ticket=%s SL %.*f -> %.*f",
                mt5_sym,
                pos.ticket,
                digits,
                current_sl,
                digits,
                candidate_sl,
            )
            continue

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": mt5_sym,
            "position": pos.ticket,
            "sl": candidate_sl,
            "tp": float(getattr(pos, "tp", 0.0) or 0.0),
        }
        res = mt5.order_send(req)
        market_closed_code = getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            if res is not None and res.retcode == market_closed_code:
                log.info("[%s] Market closed while updating SL for ticket %s; will retry.", mt5_sym, pos.ticket)
            else:
                log.error("[%s] Trail SL update failed for ticket %s: %s", mt5_sym, pos.ticket, res)
        else:
            last_trailed_bar[pos.ticket] = bar_ts
            log.info(
                "[TRAIL SL UPDATED] %s ticket=%s SL %.*f -> %.*f",
                mt5_sym,
                pos.ticket,
                digits,
                current_sl,
                digits,
                candidate_sl,
            )


def write_bot_state(state_file: str, payload: dict) -> None:
    """Atomically write the latest bot heartbeat/runtime snapshot to JSON."""
    if not state_file:
        return
    tmp_path = f"{state_file}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp_path, state_file)
    except OSError as exc:
        log.warning("Could not write bot state snapshot (%s): %s", state_file, exc)


def replay_recent_bars(
    symbol_map: Dict[str, str],
    balance: float,
    risk_pct: float,
    donchian_period: int,
    sl_atr_mult: float,
    max_cost_ratio: float,
    replay_bars: int,
    server_offset_hours: int = 7,
) -> int:
    """Scan the last `replay_bars` closed MT5 1H bars in dry-run mode (for weekend checks)."""
    n_1h = max(500, replay_bars + 300)
    signals_found = 0
    bars_checked = 0
    now_ny = datetime.now(timezone.utc).astimezone(NY_TZ)

    for _, mt5_sym in symbol_map.items():
        rates_1h = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_H1, 0, n_1h)
        if rates_1h is None or len(rates_1h) < 260:
            log.warning("[%s] Insufficient replay bars (1h=%s)", mt5_sym, len(rates_1h or []))
            continue

        raw_h1 = _closed_1h_bars(rates_1h, now_ny=now_ny, server_offset_hours=server_offset_hours)
        df1h, df4h = prepare_live_donchian_frames(raw_h1, donchian_period=donchian_period)

        start_idx = max(210, len(df1h) - replay_bars)
        log.info(
            "[%s] Replaying %d closed 1H bars (%s -> %s)",
            mt5_sym,
            len(df1h) - start_idx,
            df1h.index[start_idx].strftime("%Y-%m-%d %H:%M %Z"),
            df1h.index[-1].strftime("%Y-%m-%d %H:%M %Z"),
        )
        for i in range(start_idx, len(df1h)):
            bar_ts = df1h.index[i]
            if not is_trading_window(bar_ts.to_pydatetime()):
                continue
            bars_checked += 1
            sig = evaluate_donchian_1h_signal(df1h.iloc[: i + 1], df4h, sl_atr_mult=sl_atr_mult)
            if sig is None:
                continue

            signals_found += 1
            direction, ref_close, sl_price, atr = sig
            log.info(
                "[REPLAY %s] SIGNAL %s on %s | close=%.5f sl=%.5f atr=%.5f",
                mt5_sym,
                direction.value,
                bar_ts.strftime("%Y-%m-%d %H:%M %Z"),
                ref_close,
                sl_price,
                atr,
            )
            _place_market_order(
                mt5_sym=mt5_sym,
                direction=direction,
                sl_distance=sl_atr_mult * atr,
                risk_amount=balance * risk_pct,
                max_cost_ratio=max_cost_ratio,
                dry_run=True,
                ref_price_override=ref_close,
            )

    log.info(
        "Replay complete: scanned %d closed 1H bars across %d symbol(s), found %d setup(s).",
        bars_checked,
        len(symbol_map),
        signals_found,
    )
    return signals_found


def run_loop(
    symbols: List[str],
    risk_pct: float,
    donchian_period: int,
    sl_atr_mult: float,
    trail_atr_mult: float,
    max_cost_ratio: float,
    max_daily_dd: float,
    poll_seconds: int,
    server_offset_hours: int,
    dry_run: bool,
    once: bool,
    allow_real: bool,
    replay_bars: int = 0,
    state_file: str = "live_state.json",
) -> None:
    if replay_bars > 0 and not dry_run:
        log.info("[SAFETY] --replay-bars forces --dry-run simulation mode (no orders will be sent).")
        dry_run = True
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize() failed: {mt5.last_error()}")

    try:
        acct = mt5.account_info()
        term = mt5.terminal_info()
        if acct is None or term is None:
            raise SystemExit("Unable to fetch MT5 account or terminal info.")

        if not allow_real and acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise SystemExit(
                "Safety block: connected account is NOT a demo account. "
                "Pass --allow-real explicitly if you intend real trading."
            )
        if not dry_run and (not term.trade_allowed or not acct.trade_allowed):
            raise SystemExit(
                "Algo Trading is disabled in MT5. Enable the 'Algo Trading' toolbar button in MT5 first."
            )

        symbol_map: Dict[str, str] = {}
        for sym in symbols:
            mt5_sym = resolve_mt5_symbol(sym)
            if not mt5_sym:
                log.warning("Could not resolve MT5 symbol for %s", sym)
            else:
                symbol_map[sym] = mt5_sym

        if not symbol_map:
            raise SystemExit("No target symbols resolved in MT5.")

        log.info(
            "Connected to MT5 account=%s (balance=$%.2f, mode=%s, dry_run=%s) | Strategy=DONCHIAN_1H(lb=%d, sl=%.1fATR, trail=%.1fATR) | Symbols=%s",
            acct.login,
            acct.balance,
            "DEMO" if acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL",
            dry_run,
            donchian_period,
            sl_atr_mult,
            trail_atr_mult,
            symbol_map,
        )

        if replay_bars > 0:
            replay_recent_bars(
                symbol_map=symbol_map,
                balance=acct.balance,
                risk_pct=risk_pct,
                donchian_period=donchian_period,
                sl_atr_mult=sl_atr_mult,
                max_cost_ratio=max_cost_ratio,
                replay_bars=replay_bars,
                server_offset_hours=server_offset_hours,
            )
            return

        last_processed_bar: Dict[str, pd.Timestamp] = {}
        last_trailed_bar: Dict[int, pd.Timestamp] = {}
        tracked_day: Optional[date] = None
        day_start_equity = acct.equity

        while True:
            acct = mt5.account_info()
            now_utc = datetime.now(timezone.utc)
            now_ny = now_utc.astimezone(NY_TZ)

            if tracked_day != now_ny.date():
                tracked_day = now_ny.date()
                day_start_equity = acct.equity

            daily_dd = (day_start_equity - acct.equity) / day_start_equity if day_start_equity > 0 else 0.0
            dd_blocked = daily_dd >= max_daily_dd
            if dd_blocked:
                log.warning("Daily drawdown limit hit (%.2f%% >= %.2f%%); pausing new entries today.", daily_dd * 100, max_daily_dd * 100)

            in_window = is_trading_window(now_ny)
            if not in_window:
                log.info("Outside Mon-Fri trading window (%s NY); waiting...", now_ny.strftime("%a %H:%M"))

            all_bot_positions = [
                p for p in (mt5.positions_get() or []) if getattr(p, "magic", None) == MAGIC_NUMBER
            ]
            write_bot_state(
                state_file,
                {
                    "pid": os.getpid(),
                    "updated_at_utc": now_utc.isoformat(),
                    "updated_at_ny": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "strategy": f"DONCHIAN_1H(lb={donchian_period}, sl={sl_atr_mult:.1f}ATR, trail={trail_atr_mult:.1f}ATR)",
                    "mode": "DEMO" if acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL",
                    "dry_run": bool(dry_run),
                    "in_window": bool(in_window),
                    "dd_blocked": bool(dd_blocked),
                    "daily_dd_pct": round(daily_dd * 100.0, 3),
                    "day_start_equity": float(day_start_equity),
                    "risk_pct": float(risk_pct),
                    "donchian_period": int(donchian_period),
                    "sl_atr_mult": float(sl_atr_mult),
                    "trail_atr_mult": float(trail_atr_mult),
                    "max_cost_ratio": float(max_cost_ratio),
                    "max_daily_dd": float(max_daily_dd),
                    "poll_seconds": int(poll_seconds),
                    "server_offset_hours": int(server_offset_hours),
                    "symbols": symbol_map,
                    "account_login": int(acct.login),
                    "account_balance": float(acct.balance),
                    "account_equity": float(acct.equity),
                    "bot_open_positions": len(all_bot_positions),
                },
            )

            for _, mt5_sym in symbol_map.items():
                rates_1h = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_H1, 0, 800)
                if rates_1h is None or len(rates_1h) < 260:
                    if in_window:
                        log.warning("[%s] Insufficient 1H bars (%s)", mt5_sym, len(rates_1h or []))
                    continue

                raw_h1 = _closed_1h_bars(rates_1h, now_ny=now_ny, server_offset_hours=server_offset_hours)
                if len(raw_h1) < 250:
                    continue
                df1h, df4h = prepare_live_donchian_frames(raw_h1, donchian_period=donchian_period)
                last_closed_ts = df1h.index[-1]

                # Always update trailing SL on open bot positions during active market hours.
                if in_window:
                    _trail_open_positions(
                        mt5_sym=mt5_sym,
                        df1h=df1h,
                        trail_atr_mult=trail_atr_mult,
                        server_offset_hours=server_offset_hours,
                        dry_run=dry_run,
                        last_trailed_bar=last_trailed_bar,
                    )

                if dd_blocked or not in_window or _get_bot_positions(mt5_sym):
                    continue
                if last_processed_bar.get(mt5_sym) == last_closed_ts:
                    continue
                last_processed_bar[mt5_sym] = last_closed_ts

                sig = evaluate_donchian_1h_signal(df1h, df4h, sl_atr_mult=sl_atr_mult)
                if sig is None:
                    log.info("[%s] 1H Bar %s: no breakout setup.", mt5_sym, last_closed_ts.strftime("%Y-%m-%d %H:%M %Z"))
                    continue

                direction, ref_close, sl_price, atr = sig
                log.info(
                    "[%s] DONCHIAN SIGNAL %s on %s | ref_close=%.5f sl=%.5f atr=%.5f",
                    mt5_sym,
                    direction.value,
                    last_closed_ts.strftime("%Y-%m-%d %H:%M %Z"),
                    ref_close,
                    sl_price,
                    atr,
                )
                _place_market_order(
                    mt5_sym=mt5_sym,
                    direction=direction,
                    sl_distance=sl_atr_mult * atr,
                    risk_amount=acct.balance * risk_pct,
                    max_cost_ratio=max_cost_ratio,
                    dry_run=dry_run,
                )

            if once:
                break
            time.sleep(poll_seconds)
    finally:
        mt5.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 Demo/Live Runner for NQ + XAUUSD 1H Donchian Trend Bot")
    parser.add_argument("--symbols", nargs="+", default=["NQ", "XAUUSD"])
    parser.add_argument("--risk-pct", type=float, default=0.01)
    parser.add_argument("--donchian-period", type=int, default=20)
    parser.add_argument("--sl-atr", type=float, default=2.0)
    parser.add_argument("--trail-atr", type=float, default=2.5)
    parser.add_argument("--max-cost", type=float, default=0.20)
    parser.add_argument("--max-daily-dd", type=float, default=0.03)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--server-offset-hours", type=int, default=7, help="Broker server clock offset ahead of NY (MetaQuotes-Demo=7)")
    parser.add_argument("--dry-run", action="store_true", help="Log signals/trails without sending orders")
    parser.add_argument("--once", action="store_true", help="Run a single scan cycle and exit")
    parser.add_argument("--replay-bars", type=int, default=0, help="Dry-run replay over last N closed MT5 1H bars (works on weekends)")
    parser.add_argument("--allow-real", action="store_true", help="Allow live non-demo MT5 accounts")
    args = parser.parse_args()

    run_loop(
        symbols=args.symbols,
        risk_pct=args.risk_pct,
        donchian_period=args.donchian_period,
        sl_atr_mult=args.sl_atr,
        trail_atr_mult=args.trail_atr,
        max_cost_ratio=args.max_cost,
        max_daily_dd=args.max_daily_dd,
        poll_seconds=args.poll_seconds,
        server_offset_hours=args.server_offset_hours,
        dry_run=args.dry_run,
        once=args.once,
        allow_real=args.allow_real,
        replay_bars=args.replay_bars,
    )


if __name__ == "__main__":
    main()
