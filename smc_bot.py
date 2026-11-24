"""15-minute Mechanical SMC Bot: 1H Trend + 15m Sweep + CHoCH + FVG Limit Entry."""

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

from config import ALL_SYMBOLS, SYMBOL_COSTS
from data_loader import load_from_csv
from models import Bias, Trade, TradeStatus
from report import compute_metrics, generate_html_report, trades_to_dataframe

NY_TZ = ZoneInfo("America/New_York")


def _detect_ltf_fvg_sweep(window_ltf: pd.DataFrame, trend: Bias) -> Optional[Tuple[Bias, float, float]]:
    """Detect a liquidity sweep + CHoCH + 3-candle FVG in the direction of `trend`."""
    w = window_ltf.iloc[-25:]
    c2, c0 = w.iloc[-3], w.iloc[-1]
    b_close = float(c0["close"])
    prior = w.iloc[:-12]
    recent = w.iloc[-12:]

    if trend == Bias.BULLISH:
        swept = float(recent["low"].min()) < float(prior["low"].min())
        has_fvg = float(c2["high"]) < float(c0["low"])
        choch = b_close > float(w.iloc[-12:-1]["high"].max())
        if swept and has_fvg and choch:
            return Bias.BULLISH, float(c0["low"]), float(recent["low"].min())
    elif trend == Bias.BEARISH:
        swept = float(recent["high"].max()) > float(prior["high"].max())
        has_fvg = float(c2["low"]) > float(c0["high"])
        choch = b_close < float(w.iloc[-12:-1]["low"].min())
        if swept and has_fvg and choch:
            return Bias.BEARISH, float(c0["high"]), float(recent["high"].max())
    return None


def evaluate_15m_smc_signal(
    window_ltf: pd.DataFrame,
    df1h: pd.DataFrame,
    ts: pd.Timestamp,
) -> Optional[Tuple[Bias, float, float]]:
    """
    Shared 1H EMA(50/200) Trend + 15m Sweep + CHoCH + FVG signal evaluator.
    Used identically by both backtester and live MT5 runner.
    Returns (direction, entry_limit_price, sl_price) or None.
    """
    if len(window_ltf) < 25 or df1h.empty:
        return None

    h1_slice = df1h[df1h.index < ts]
    if h1_slice.empty:
        return None
    h1_last = h1_slice.iloc[-1]
    if h1_last["ema50"] > h1_last["ema200"] and h1_last["close"] > h1_last["ema50"]:
        trend = Bias.BULLISH
    elif h1_last["ema50"] < h1_last["ema200"] and h1_last["close"] < h1_last["ema50"]:
        trend = Bias.BEARISH
    else:
        return None

    return _detect_ltf_fvg_sweep(window_ltf, trend)


def run_smc_backtest(
    data_5m: Dict[str, pd.DataFrame],
    start_date: str,
    end_date: str,
    rr: float = 2.0,
    risk_pct: float = 0.01,
    initial_capital: float = 100_000.0,
    apply_costs: bool = True,
    ltf: str = "15min",
    max_cost_ratio: float = 0.20,
    require_4h_trend: bool = False,
    swing_mode: bool = False,
) -> Tuple[List[Trade], List[Tuple[datetime, float]]]:
    """
    Purely mechanical SMC strategy:
    1. 1H (and optional 4H) Trend: EMA(50) > EMA(200) & close > EMA(50) -> BULLISH.
    2. LTF Sweep + CHoCH + FVG:
       - Price swept a prior LTF swing low/high within the last 12 bars.
       - Current LTF candle closes past the opposing swing (CHoCH) AND
         forms a 3-candle FVG in the impulse.
    3. Limit entry at FVG edge (0 entry slippage), SL at swept swing extreme, TP = RR.
    4. Active Mon-Fri 02:00-16:00 NY (London + NY), blackout Dec 27-31.
    """
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    start_ts = pd.Timestamp(start_date, tz=NY_TZ)
    end_ts = pd.Timestamp(end_date, tz=NY_TZ) + pd.Timedelta(days=1)

    prepared: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    for sym, df5 in data_5m.items():
        df_ltf = df5.resample(ltf).agg(agg).dropna()
        df1h = df5.resample("1h").agg(agg).dropna()
        df4h = df5.resample("4h").agg(agg).dropna()
        df1d = df5.resample("1D").agg(agg).dropna()
        if len(df4h) < 60 or len(df_ltf) < 50 or len(df1h) < (60 if swing_mode else 210):
            continue
        df1h["ema50"] = df1h["close"].ewm(span=50, adjust=False).mean()
        df1h["ema200"] = df1h["close"].ewm(span=200, adjust=False).mean()
        df4h["ema50"] = df4h["close"].ewm(span=50, adjust=False).mean()
        df4h["ema200"] = df4h["close"].ewm(span=200, adjust=False).mean()
        df1d["ema20"] = df1d["close"].ewm(span=20, adjust=False).mean()
        prepared[sym] = (df_ltf, df1h, df4h, df1d)

    all_times = sorted(
        {
            ts
            for df_ltf, _, _, _ in prepared.values()
            for ts in df_ltf.index
            if start_ts <= ts < end_ts
        }
    )

    pos_ltf = {sym: {ts: i for i, ts in enumerate(df_ltf.index)} for sym, (df_ltf, _, _, _) in prepared.items()}
    equity = initial_capital
    equity_curve: List[Tuple[datetime, float]] = []
    trades: List[Trade] = []
    next_id = 1

    for ts in all_times:
        ts_dt = ts.to_pydatetime()
        # Mon-Fri only, Dec 27-31 blackout; swing mode trades 24h Mon-Fri, intraday 02:00-16:00 NY
        is_trading_time = (
            ts_dt.weekday() < 5
            and (date(ts_dt.year, 12, 31) - ts_dt.date()).days >= 5
            and (swing_mode or 2 <= ts_dt.hour < 16)
        )

        for sym, (df_ltf, df1h, df4h, df1d) in prepared.items():
            idx = pos_ltf[sym].get(ts)
            if idx is None or idx < 30:
                continue

            bar = df_ltf.iloc[idx]
            b_high = float(bar["high"])
            b_low = float(bar["low"])
            b_close = float(bar["close"])

            # 1. Manage existing pending/open trades for this symbol
            active_trade: Optional[Trade] = None
            for t in trades:
                if t.symbol != sym or t.status not in (TradeStatus.PENDING, TradeStatus.OPEN):
                    continue
                if t.status == TradeStatus.PENDING:
                    if ts_dt >= t.expires_at:
                        t.status = TradeStatus.CANCELLED
                        t.exit_reason = "EXPIRED"
                        t.closed_at = ts_dt
                        continue
                    if b_low <= t.entry_price <= b_high:
                        t.status = TradeStatus.OPEN
                        t.filled_at = ts_dt

                if t.status == TradeStatus.OPEN:
                    hit_sl = (b_low <= t.current_sl) if t.direction == Bias.BULLISH else (b_high >= t.current_sl)
                    hit_tp = (b_high >= t.tp_price) if t.direction == Bias.BULLISH else (b_low <= t.tp_price)
                    if hit_sl:
                        equity += _settle_trade(t, t.current_sl, ts_dt, "SL", apply_costs, is_sl=True)
                        continue
                    if hit_tp:
                        equity += _settle_trade(t, t.tp_price, ts_dt, "TP", apply_costs, is_sl=False)
                        continue
                    active_trade = t

            if active_trade is not None or any(
                t.symbol == sym and t.status == TradeStatus.PENDING for t in trades
            ):
                continue

            if not is_trading_time:
                continue

            # 2 & 3. HTF Trend + LTF Sweep + CHoCH + FVG
            w = df_ltf.iloc[idx - 24 : idx + 1]
            if not swing_mode and not require_4h_trend:
                sig = evaluate_15m_smc_signal(w, df1h, ts)
                if sig is None:
                    continue
                signal_dir, entry_price, sl_price = sig
            else:
                if swing_mode:
                    h4_slice = df4h[df4h.index < ts]
                    d1_slice = df1d[df1d.index < ts.normalize()]
                    if len(h4_slice) < 20 or len(d1_slice) < 5:
                        continue
                    h4_last = h4_slice.iloc[-1]
                    d1_last = d1_slice.iloc[-1]
                    if (
                        h4_last["ema50"] > h4_last["ema200"]
                        and h4_last["close"] > h4_last["ema50"]
                        and d1_last["close"] > d1_last["ema20"]
                    ):
                        trend = Bias.BULLISH
                    elif (
                        h4_last["ema50"] < h4_last["ema200"]
                        and h4_last["close"] < h4_last["ema50"]
                        and d1_last["close"] < d1_last["ema20"]
                    ):
                        trend = Bias.BEARISH
                    else:
                        continue
                else:
                    h1_slice = df1h[df1h.index < ts]
                    if h1_slice.empty:
                        continue
                    h1_last = h1_slice.iloc[-1]
                    if h1_last["ema50"] > h1_last["ema200"] and h1_last["close"] > h1_last["ema50"]:
                        trend = Bias.BULLISH
                    elif h1_last["ema50"] < h1_last["ema200"] and h1_last["close"] < h1_last["ema50"]:
                        trend = Bias.BEARISH
                    else:
                        continue
                    h4_slice = df4h[df4h.index < ts]
                    if h4_slice.empty:
                        continue
                    h4_last = h4_slice.iloc[-1]
                    if trend == Bias.BULLISH and not (h4_last["ema50"] > h4_last["ema200"] and h4_last["close"] > h4_last["ema50"]):
                        continue
                    if trend == Bias.BEARISH and not (h4_last["ema50"] < h4_last["ema200"] and h4_last["close"] < h4_last["ema50"]):
                        continue

                sig = _detect_ltf_fvg_sweep(w, trend)
                if sig is None:
                    continue
                signal_dir, entry_price, sl_price = sig

            risk_dist = abs(entry_price - sl_price)
            if risk_dist <= 0:
                continue

            risk_amount = equity * risk_pct
            size = risk_amount / risk_dist
            if apply_costs:
                sc = SYMBOL_COSTS.get(sym)
                if sc is not None:
                    est_costs = (sc.spread + sc.slippage_per_side) * size + (
                        2.0 * sc.commission_rate * entry_price * size
                    )
                    if est_costs > risk_amount * max_cost_ratio:
                        continue

            tp_price = (
                entry_price + risk_dist * rr
                if signal_dir == Bias.BULLISH
                else entry_price - risk_dist * rr
            )
            exp_hours = 6 if swing_mode else (3 if ltf == "30min" else 2)
            trades.append(
                Trade(
                    trade_id=next_id,
                    symbol=sym,
                    session="SWING" if swing_mode else "LDN_NY",
                    direction=signal_dir,
                    entry_price=entry_price,
                    initial_sl=sl_price,
                    current_sl=sl_price,
                    tp_price=tp_price,
                    risk_pct=risk_pct,
                    risk_amount=risk_amount,
                    size=size,
                    placed_at=ts_dt,
                    expires_at=ts_dt + timedelta(hours=exp_hours),
                    notes=f"smc_{ltf}_fvg",
                )
            )
            next_id += 1

        equity_curve.append((ts_dt, equity))

    return trades, equity_curve


def _settle_trade(
    trade: Trade, exit_price: float, ts: datetime, reason: str, apply_costs: bool, is_sl: bool
) -> float:
    trade.status = TradeStatus.CLOSED
    trade.exit_price = exit_price
    trade.closed_at = ts
    trade.exit_reason = reason

    gross = (
        (exit_price - trade.entry_price) * trade.size
        if trade.direction == Bias.BULLISH
        else (trade.entry_price - exit_price) * trade.size
    )
    costs = 0.0
    if apply_costs:
        sc = SYMBOL_COSTS.get(trade.symbol)
        if sc is not None:
            # Limit entry & TP have 0 slippage; SL market exit has 1x slippage
            slip = sc.slippage_per_side if is_sl else 0.0
            costs = (sc.spread + slip) * trade.size + 2.0 * sc.commission_rate * trade.entry_price * trade.size

    trade.costs = costs
    trade.pnl = gross - costs
    trade.rr_realized = trade.pnl / trade.risk_amount if trade.risk_amount > 0 else 0.0
    return trade.pnl


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 15m Mechanical SMC Backtest")
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out-prefix", required=True)
    parser.add_argument("--symbols", nargs="+", default=ALL_SYMBOLS)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--ltf", default="15min")
    parser.add_argument("--max-cost", type=float, default=0.20)
    parser.add_argument("--require-4h", action="store_true")
    parser.add_argument("--swing", action="store_true", help="Daily+4H trend with 1H entries 24/5")
    args = parser.parse_args()

    data_5m: Dict[str, pd.DataFrame] = {}
    for sym in args.symbols:
        p = args.csv_dir / f"{sym}.csv"
        if p.exists():
            df5, _, _ = load_from_csv(p)
            data_5m[sym] = df5

    trades, eq = run_smc_backtest(
        data_5m,
        args.start,
        args.end,
        rr=args.rr,
        ltf="1h" if args.swing else args.ltf,
        max_cost_ratio=args.max_cost,
        require_4h_trend=args.require_4h,
        swing_mode=args.swing,
    )
    metrics = compute_metrics(trades, 100_000.0)
    print(f"\n=== {args.out_prefix} (15m Mechanical SMC) ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    df = trades_to_dataframe(trades)
    if not df.empty:
        df.to_csv(f"{args.out_prefix}.csv", index=False)
    generate_html_report(trades, eq, 100_000.0, f"{args.out_prefix}.html")


if __name__ == "__main__":
    main()
