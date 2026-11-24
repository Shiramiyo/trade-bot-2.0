"""Backtest performance metrics and interactive Plotly HTML report."""

from pathlib import Path
from typing import Dict, List, Tuple
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from models import Trade, TradeStatus


def compute_metrics(trades: List[Trade], initial_capital: float) -> Dict[str, float]:
    """Compute core trading performance metrics from closed trades."""
    closed = [t for t in trades if t.status == TradeStatus.CLOSED]
    if not closed:
        return {
            "total_trades": 0,
            "win_rate_pct": 0.0,
            "net_pnl": 0.0,
            "return_pct": 0.0,
            "profit_factor": 0.0,
            "avg_rr": 0.0,
            "max_drawdown_pct": 0.0,
        }

    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl < 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    net_pnl = sum(t.pnl for t in closed)

    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0
    for t in sorted(closed, key=lambda x: x.closed_at or x.placed_at):
        equity += t.pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len([t for t in closed if t.pnl == 0]),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2),
        "net_pnl": round(net_pnl, 2),
        "total_costs": round(sum(t.costs for t in closed), 2),
        "return_pct": round(net_pnl / initial_capital * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_rr": round(sum(t.rr_realized for t in closed) / len(closed), 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }


def trades_to_dataframe(trades: List[Trade]) -> pd.DataFrame:
    """Convert trade list into a clean pandas DataFrame."""
    rows = []
    for t in trades:
        rows.append(
            {
                "id": t.trade_id,
                "symbol": t.symbol,
                "session": t.session,
                "direction": t.direction.value,
                "status": t.status.value,
                "entry": round(t.entry_price, 5),
                "sl": round(t.initial_sl, 5),
                "tp": round(t.tp_price, 5),
                "exit": round(t.exit_price, 5) if t.exit_price is not None else None,
                "exit_reason": t.exit_reason,
                "risk_pct": t.risk_pct * 100,
                "costs": round(t.costs, 2),
                "pnl": round(t.pnl, 2),
                "rr": round(t.rr_realized, 2),
                "placed_at": t.placed_at,
                "filled_at": t.filled_at,
                "closed_at": t.closed_at,
                "notes": t.notes,
            }
        )
    return pd.DataFrame(rows)


def generate_html_report(
    trades: List[Trade],
    equity_curve: List[Tuple],
    initial_capital: float,
    output_path: str | Path = "backtest_report.html",
) -> Path:
    """Save an interactive Plotly HTML report with equity curve & symbol breakdown."""
    out = Path(output_path)
    closed = [t for t in trades if t.status == TradeStatus.CLOSED]

    fig = make_subplots(
        rows=2,
        cols=1,
        subplot_titles=("Equity Curve", "Net PnL by Symbol"),
        vertical_spacing=0.15,
    )

    if equity_curve:
        times, eqs = zip(*equity_curve)
        fig.add_trace(
            go.Scatter(x=list(times), y=list(eqs), mode="lines", name="Equity"),
            row=1,
            col=1,
        )

    if closed:
        df = trades_to_dataframe(closed)
        by_sym = df.groupby("symbol")["pnl"].sum().sort_values(ascending=False)
        fig.add_trace(
            go.Bar(x=by_sym.index.tolist(), y=by_sym.values.tolist(), name="PnL by Symbol"),
            row=2,
            col=1,
        )

    metrics = compute_metrics(trades, initial_capital)
    fig.update_layout(
        title=(
            f"Backtest Report | Trades: {metrics['total_trades']} | "
            f"Win Rate: {metrics['win_rate_pct']}% | "
            f"Net PnL: ${metrics['net_pnl']:,.2f} ({metrics['return_pct']}%) | "
            f"Max DD: {metrics['max_drawdown_pct']}%"
        ),
        height=750,
        template="plotly_dark",
    )
    fig.write_html(str(out))
    return out
