"""Build news_usd_2025_2026.csv and backtest mechanical USD-news-only strategies (no BE)."""

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo
import pandas as pd

from config import SYMBOL_COSTS
from data_loader import load_from_csv
from report import compute_metrics, generate_html_report, trades_to_dataframe
from models import Bias, Trade, TradeStatus

NY_TZ = ZoneInfo("America/New_York")

# Verified 2025 & 2026 US CPI (08:30 NY) and FOMC Decision (14:00 NY) dates
CPI_DATES = [
    # 2025
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10", "2025-05-14",
    "2025-06-11", "2025-07-11", "2025-08-13", "2025-09-10", "2025-10-15",
    "2025-12-16",
    # 2026
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12",
    "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14",
    "2026-11-10",
]

FOMC_DATES = [
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28",
]


def build_usd_news_events() -> List[Tuple[datetime, str]]:
    """Return sorted list of (NY datetime, title) for 2025-2026 CPI, FOMC, and NFP."""
    events: List[Tuple[datetime, str]] = []

    for d_str in CPI_DATES:
        d = date.fromisoformat(d_str)
        events.append((datetime(d.year, d.month, d.day, 8, 30, tzinfo=NY_TZ), "US CPI"))

    for d_str in FOMC_DATES:
        d = date.fromisoformat(d_str)
        events.append((datetime(d.year, d.month, d.day, 14, 0, tzinfo=NY_TZ), "FOMC"))

    # First Friday of every month in 2025-2026 = NFP (08:30 NY)
    for year in (2025, 2026):
        for month in range(1, 13):
            if year == 2026 and month > 11:
                continue
            d = date(year, month, 1)
            while d.weekday() != 4:  # Friday
                d += timedelta(days=1)
            events.append((datetime(d.year, d.month, d.day, 8, 30, tzinfo=NY_TZ), "US NFP"))

    events.sort(key=lambda x: x[0])
    return events


def run_news_strategy(
    data_5m: Dict[str, pd.DataFrame],
    events: List[Tuple[datetime, str]],
    mode: str,
    start_date: str,
    end_date: str,
    rr: float = 2.0,
    risk_pct: float = 0.01,
    initial_capital: float = 100_000.0,
    apply_costs: bool = True,
) -> Tuple[List[Trade], List[Tuple[datetime, float]]]:
    """
    Simulate mechanical USD-news-only strategy (no BE, let trade play out to SL/TP or 16:30 NY).
    mode='sweep_reverse': 15m candle sweeps 6h pre-news H/L and closes back inside.
    mode='momentum_breakout': Enter on close of first 15m news candle in its direction.
    """
    start_dt = pd.Timestamp(start_date, tz=NY_TZ).to_pydatetime()
    end_dt = (pd.Timestamp(end_date, tz=NY_TZ) + pd.Timedelta(days=1)).to_pydatetime()
    filtered_events = [(ts, title) for ts, title in events if start_dt <= ts < end_dt]

    # Resample 5m -> 15m for clean mechanical signals
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    data_15m = {sym: df.resample("15min").agg(agg).dropna() for sym, df in data_5m.items()}

    equity = initial_capital
    equity_curve: List[Tuple[datetime, float]] = []
    trades: List[Trade] = []
    trade_id = 1

    for news_ts, title in filtered_events:
        # Year-end blackout (last 5 days of year)
        if (date(news_ts.year, 12, 31) - news_ts.date()).days < 5:
            continue

        eod_ts = datetime(news_ts.year, news_ts.month, news_ts.day, 16, 30, tzinfo=NY_TZ)
        if news_ts.hour >= 14:
            eod_ts = datetime(news_ts.year, news_ts.month, news_ts.day, 17, 0, tzinfo=NY_TZ)

        for sym, df15 in data_15m.items():
            pre = df15[(df15.index >= news_ts - timedelta(hours=6)) & (df15.index < news_ts)]
            post = df15[(df15.index >= news_ts) & (df15.index <= eod_ts)]
            if len(pre) < 4 or len(post) < 2:
                continue

            pre_high = float(pre["high"].max())
            pre_low = float(pre["low"].min())

            entry_idx = None
            direction = Bias.NONE
            entry_price = 0.0
            sl_price = 0.0

            if mode == "momentum_breakout":
                c0 = post.iloc[0]
                if c0["close"] > c0["open"]:
                    direction = Bias.BULLISH
                    entry_price = float(c0["close"])
                    sl_price = float(c0["low"])
                    entry_idx = 1
                elif c0["close"] < c0["open"]:
                    direction = Bias.BEARISH
                    entry_price = float(c0["close"])
                    sl_price = float(c0["high"])
                    entry_idx = 1

            elif mode == "sweep_reverse":
                # Watch first 6 15m bars (90 mins) for sweep + close back inside
                for k in range(min(6, len(post) - 1)):
                    ck = post.iloc[k]
                    if float(ck["high"]) > pre_high and float(ck["close"]) < pre_high:
                        direction = Bias.BEARISH
                        entry_price = float(ck["close"])
                        sl_price = float(ck["high"])
                        entry_idx = k + 1
                        break
                    if float(ck["low"]) < pre_low and float(ck["close"]) > pre_low:
                        direction = Bias.BULLISH
                        entry_price = float(ck["close"])
                        sl_price = float(ck["low"])
                        entry_idx = k + 1
                        break

            if direction == Bias.NONE or entry_idx is None or entry_idx >= len(post):
                continue

            risk_dist = abs(entry_price - sl_price)
            if risk_dist <= 0:
                continue

            tp_price = (
                entry_price + risk_dist * rr
                if direction == Bias.BULLISH
                else entry_price - risk_dist * rr
            )
            risk_amount = equity * risk_pct
            size = risk_amount / risk_dist

            # Cost guard: skip if spread+slip+comm > 20% of risk
            sc = SYMBOL_COSTS.get(sym)
            costs = 0.0
            if apply_costs and sc is not None:
                costs = (sc.spread + 2.0 * sc.slippage_per_side) * size + (
                    2.0 * sc.commission_rate * entry_price * size
                )
                if costs > risk_amount * 0.20:
                    continue

            fill_ts = post.index[entry_idx - 1].to_pydatetime()
            exit_price = float(post.iloc[-1]["close"])
            exit_ts = post.index[-1].to_pydatetime()
            exit_reason = "EOD"

            for j in range(entry_idx, len(post)):
                bar = post.iloc[j]
                if direction == Bias.BULLISH:
                    if float(bar["low"]) <= sl_price:
                        exit_price = sl_price
                        exit_ts = post.index[j].to_pydatetime()
                        exit_reason = "SL"
                        break
                    if float(bar["high"]) >= tp_price:
                        exit_price = tp_price
                        exit_ts = post.index[j].to_pydatetime()
                        exit_reason = "TP"
                        break
                else:
                    if float(bar["high"]) >= sl_price:
                        exit_price = sl_price
                        exit_ts = post.index[j].to_pydatetime()
                        exit_reason = "SL"
                        break
                    if float(bar["low"]) <= tp_price:
                        exit_price = tp_price
                        exit_ts = post.index[j].to_pydatetime()
                        exit_reason = "TP"
                        break

            gross = (
                (exit_price - entry_price) * size
                if direction == Bias.BULLISH
                else (entry_price - exit_price) * size
            )
            pnl = gross - costs
            equity += pnl
            equity_curve.append((exit_ts, equity))

            trades.append(
                Trade(
                    trade_id=trade_id,
                    symbol=sym,
                    session="USD_NEWS",
                    direction=direction,
                    entry_price=entry_price,
                    initial_sl=sl_price,
                    current_sl=sl_price,
                    tp_price=tp_price,
                    risk_pct=risk_pct,
                    risk_amount=risk_amount,
                    size=size,
                    placed_at=fill_ts,
                    expires_at=fill_ts,
                    status=TradeStatus.CLOSED,
                    filled_at=fill_ts,
                    closed_at=exit_ts,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    costs=costs,
                    pnl=pnl,
                    rr_realized=pnl / risk_amount if risk_amount > 0 else 0.0,
                    notes=f"{title} ({mode})",
                )
            )
            trade_id += 1

    return trades, equity_curve


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--mode", choices=["sweep_reverse", "momentum_breakout"], required=True)
    parser.add_argument("--out-prefix", required=True)
    args = parser.parse_args()

    symbols = ["XAUUSD", "USDCAD", "AUDUSD", "BTCUSD"]
    data_5m: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        p = args.csv_dir / f"{sym}.csv"
        if p.exists():
            df_5m, _, _ = load_from_csv(p)
            data_5m[sym] = df_5m

    events = build_usd_news_events()
    trades, eq = run_news_strategy(data_5m, events, args.mode, args.start, args.end)
    metrics = compute_metrics(trades, 100_000.0)
    print(f"\n=== {args.out_prefix} ({args.mode}) ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    df = trades_to_dataframe(trades)
    if not df.empty:
        df.to_csv(f"{args.out_prefix}.csv", index=False)
    generate_html_report(trades, eq, 100_000.0, f"{args.out_prefix}.html")


if __name__ == "__main__":
    main()
