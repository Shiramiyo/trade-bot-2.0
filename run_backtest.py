"""CLI entry point to run a backtest on yfinance or local CSV data."""

import argparse
from pathlib import Path
from typing import Dict, Tuple
import pandas as pd

from config import ALL_SYMBOLS, StrategyConfig
from data_loader import load_from_csv, load_universe_yfinance
from news_calendar import NewsCalendar
from backtester import Backtester
from report import compute_metrics, generate_html_report, trades_to_dataframe


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ICT/SMC Strategy Backtest")
    parser.add_argument("--source", choices=["yfinance", "csv"], default="yfinance")
    parser.add_argument("--period", default="59d", help="yfinance lookback period (e.g. 59d, 30d)")
    parser.add_argument("--csv-dir", type=Path, help="Directory with <SYMBOL>.csv 5m OHLCV files")
    parser.add_argument("--news", type=Path, help="Optional high-impact news CSV file")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--symbols", nargs="+", default=ALL_SYMBOLS)
    parser.add_argument("--start", help="Optional backtest start date YYYY-MM-DD (e.g. 2025-01-01)")
    parser.add_argument("--end", help="Optional backtest end date YYYY-MM-DD (e.g. 2025-12-31)")
    parser.add_argument("--no-costs", action="store_true", help="Disable spread/slippage/commission costs")
    parser.add_argument("--news-only", action="store_true", help="Only trade on high-impact news days")
    parser.add_argument("--let-play-out", action="store_true", help="Disable BE and early CISD cut")
    parser.add_argument("--out-html", type=Path, default=Path("backtest_report.html"))
    parser.add_argument("--out-trades", type=Path, default=Path("trades.csv"))
    args = parser.parse_args()

    cfg = StrategyConfig(
        initial_capital=args.capital,
        symbols=args.symbols,
        apply_costs=not args.no_costs,
        news_only=args.news_only,
        let_play_out=args.let_play_out,
    )
    news = NewsCalendar.from_csv(args.news) if args.news else NewsCalendar()

    data: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    if args.source == "yfinance":
        print(f"Downloading 5m data via yfinance ({args.period}) for {len(args.symbols)} symbols...")
        data = load_universe_yfinance(args.symbols, period=args.period)
    else:
        if not args.csv_dir:
            raise SystemExit("--csv-dir is required when --source csv")
        for sym in args.symbols:
            p = args.csv_dir / f"{sym}.csv"
            if p.exists():
                data[sym] = load_from_csv(p)
            else:
                print(f"[WARN] Missing CSV: {p}")

    if not data:
        raise SystemExit("No market data loaded. Exiting.")

    if args.start or args.end:
        from data_loader import NY_TZ, resample_timeframes

        start_ts = pd.Timestamp(args.start, tz=NY_TZ) - pd.Timedelta(days=5) if args.start else None
        end_ts = pd.Timestamp(args.end, tz=NY_TZ) + pd.Timedelta(days=1) if args.end else None
        filtered: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
        for sym, (df_5m, _, _) in data.items():
            sub = df_5m
            if start_ts is not None:
                sub = sub[sub.index >= start_ts]
            if end_ts is not None:
                sub = sub[sub.index < end_ts]
            if not sub.empty:
                filtered[sym] = resample_timeframes(sub)
        data = filtered

    bt = Backtester(data=data, cfg=cfg, news=news)
    trades = bt.run()

    if args.start:
        from data_loader import NY_TZ

        real_start = pd.Timestamp(args.start, tz=NY_TZ).to_pydatetime()
        trades = [t for t in trades if t.placed_at >= real_start]

    metrics = compute_metrics(trades, cfg.initial_capital)
    print("\n=== BACKTEST RESULTS ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    df_trades = trades_to_dataframe(trades)
    if not df_trades.empty:
        df_trades.to_csv(args.out_trades, index=False)
        print(f"\nSaved trades log to {args.out_trades}")

    report_path = generate_html_report(trades, bt.equity_curve, cfg.initial_capital, args.out_html)
    print(f"Saved interactive report to {report_path}")


if __name__ == "__main__":
    main()
