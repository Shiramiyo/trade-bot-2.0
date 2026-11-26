"""
Paper-only 15-minute BTC directional prediction research & walk-forward evaluator.

Educational quantitative research tool only — no live exchange orders or real funds.
Fetches closed 15m BTCUSDT candles (Binance Futures primary with Hyperliquid fallback),
builds strictly causal features, runs rolling chronological walk-forward cross-validation
against naive baselines, and measures how transaction costs affect short-horizon edges.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import urllib.request

import numpy as np
import pandas as pd


FEATURE_COLUMNS: Tuple[str, ...] = (
    "ret_1",
    "ret_2",
    "ret_4",
    "ret_12",
    "ret_32",
    "body_ratio",
    "close_pos_in_range",
    "range_over_atr",
    "vol_z_20",
    "taker_imbalance_1",
    "taker_imbalance_4",
    "ema_dist_12",
    "ema_dist_48",
    "rsi_6",
    "rsi_14",
    "tod_sin",
    "tod_cos",
)


def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "TradeTerminalResearch/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, payload: Any, timeout: float = 10.0) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "TradeTerminalResearch/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_btc_15m_candles(
    total_bars: int = 3000,
    symbol: str = "BTCUSDT",
    http_getter: Callable[[str], Any] = _http_get_json,
    http_poster: Callable[[str, Any], Any] = _http_post_json,
    interval: str = "15m",
) -> Tuple[pd.DataFrame, str]:
    """Fetch closed 5m or 15m candles in chronological order from Binance Futures or Hyperliquid."""
    iv = interval if interval in ("5m", "15m") else "15m"
    iv_mins = 5 if iv == "5m" else 15
    target = max(400, min(6000, int(total_bars)))
    df = _fetch_binance_15m_paginated(target, symbol=symbol, http_getter=http_getter, interval=iv)
    source = f"Binance Futures ({symbol} {iv})"
    if df.empty:
        coin = symbol[:-4] if symbol.endswith("USDT") else symbol
        df = _fetch_hyperliquid_15m(target, coin=coin, http_poster=http_poster, interval=iv)
        source = f"Hyperliquid ({coin}-USD {iv})"
    if df.empty:
        raise RuntimeError(f"Unable to fetch {iv} candles for {symbol} from Binance or Hyperliquid")

    # Drop the latest bar if it has not finished its interval close yet
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if len(df) >= 2 and int(df["open_time_ms"].iloc[-1]) + iv_mins * 60 * 1000 > now_ms:
        df = df.iloc[:-1].copy()
    return df.reset_index(drop=True), source


def _fetch_binance_15m_paginated(
    target_bars: int,
    symbol: str,
    http_getter: Callable[[str], Any],
    interval: str = "15m",
) -> pd.DataFrame:
    chunks: List[List[Any]] = []
    end_ms: Optional[int] = None
    remaining = target_bars + 2  # buffer for dropping unclosed tip

    while remaining > 0:
        batch = min(1000, remaining)
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={batch}"
        if end_ms is not None:
            url += f"&endTime={end_ms}"
        try:
            raw = http_getter(url)
        except Exception:
            break
        if not raw:
            break
        chunks.append(raw)
        first_open = int(raw[0][0])
        end_ms = first_open - 1
        remaining -= len(raw)
        if len(raw) < batch:
            break

    if not chunks:
        return pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    for chunk in reversed(chunks):
        for k in chunk:
            rows.append(
                {
                    "open_time_ms": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "quote_volume": float(k[7]),
                    "trades": int(k[8]),
                    "taker_buy_vol": float(k[9]),
                }
            )
    df = pd.DataFrame(rows).drop_duplicates(subset=["open_time_ms"]).sort_values("open_time_ms")
    df["timestamp"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
    return df.tail(target_bars + 1).reset_index(drop=True)


def _fetch_hyperliquid_15m(
    target_bars: int,
    coin: str,
    http_poster: Callable[[str, Any], Any],
    interval: str = "15m",
) -> pd.DataFrame:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    iv_mins = 5 if interval == "5m" else 15
    bar_ms = iv_mins * 60 * 1000
    start_ms = end_ms - (target_bars + 5) * bar_ms
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
    }
    try:
        raw = http_poster("https://api.hyperliquid.xyz/info", payload)
    except Exception:
        return pd.DataFrame()
    if not raw:
        return pd.DataFrame()

    rows = [
        {
            "open_time_ms": int(k["t"]),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k.get("v", 0.0) or 0.0),
            "quote_volume": float(k.get("v", 0.0) or 0.0) * float(k["c"]),
            "trades": int(k.get("n", 0) or 0),
            "taker_buy_vol": 0.5 * float(k.get("v", 0.0) or 0.0),  # neutral fallback when taker split absent
        }
        for k in raw
        if isinstance(k, dict) and "t" in k
    ]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset=["open_time_ms"]).sort_values("open_time_ms")
    df["timestamp"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
    return df.tail(target_bars + 1).reset_index(drop=True)


def _compute_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return (rsi - 50.0) / 50.0


def build_causal_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build strictly causal features at bar t to predict next bar (t+1) return/direction.

    Invariant: No feature at index `t` ever reads data at index `> t`.
    """
    work = df.copy()
    close = work["close"].astype(float)
    open_ = work["open"].astype(float)
    high = work["high"].astype(float)
    low = work["low"].astype(float)
    vol = work["volume"].astype(float).clip(lower=0.0)
    taker_buy = work.get("taker_buy_vol", pd.Series(0.5 * vol, index=work.index)).astype(float).clip(lower=0.0)

    eps = 1e-12
    bar_range = (high - low).clip(lower=eps)
    prev_close = close.shift(1).fillna(close)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = tr.rolling(14, min_periods=14).mean().clip(lower=eps)

    # Returns over multiple causal horizons
    work["ret_1"] = close.pct_change(1)
    work["ret_2"] = close.pct_change(2)
    work["ret_4"] = close.pct_change(4)
    work["ret_12"] = close.pct_change(12)
    work["ret_32"] = close.pct_change(32)

    # Intrabar pressure and volatility structure
    work["body_ratio"] = (close - open_) / bar_range
    work["close_pos_in_range"] = ((close - low) / bar_range) * 2.0 - 1.0
    work["range_over_atr"] = bar_range / atr14

    # Volume & order-flow taker imbalance in [-1, 1]
    log_vol = np.log1p(vol)
    vol_mean = log_vol.rolling(20, min_periods=20).mean()
    vol_std = log_vol.rolling(20, min_periods=20).std().clip(lower=1e-6)
    work["vol_z_20"] = ((log_vol - vol_mean) / vol_std).clip(-4.0, 4.0)

    imb = (2.0 * (taker_buy / vol.replace(0.0, np.nan)) - 1.0).fillna(0.0).clip(-1.0, 1.0)
    work["taker_imbalance_1"] = imb
    vol4 = vol.rolling(4, min_periods=4).sum().replace(0.0, np.nan)
    tb4 = taker_buy.rolling(4, min_periods=4).sum()
    work["taker_imbalance_4"] = (2.0 * (tb4 / vol4) - 1.0).fillna(0.0).clip(-1.0, 1.0)

    # Trend / mean-reversion oscillators
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema48 = close.ewm(span=48, adjust=False).mean()
    work["ema_dist_12"] = close / ema12 - 1.0
    work["ema_dist_48"] = close / ema48 - 1.0
    work["rsi_6"] = _compute_rsi(close, 6)
    work["rsi_14"] = _compute_rsi(close, 14)

    # Cyclical UTC time-of-day session signal
    ts = pd.to_datetime(work["timestamp"], utc=True)
    hour_frac = ts.dt.hour + ts.dt.minute / 60.0
    work["tod_sin"] = np.sin(2.0 * math.pi * hour_frac / 24.0)
    work["tod_cos"] = np.cos(2.0 * math.pi * hour_frac / 24.0)

    # Target for bar t is bar (t+1) return (shifted -1 ONLY on label columns!)
    work["next_close"] = close.shift(-1)
    work["next_ret"] = work["next_close"] / close - 1.0
    work["target_up"] = (work["next_ret"] > 0.0).astype(float)
    work.loc[work["next_ret"].isna(), "target_up"] = np.nan
    return work


def _sigmoid(z: np.ndarray) -> np.ndarray:
    clipped = np.clip(z, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_standardizer(X_train: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Compute column mean and std on training slice only (no future leakage)."""
    mean = X_train.mean(axis=0)
    std = X_train.std(axis=0)
    std = np.where(std < 1e-8, 1.0, std)
    return mean, std


def fit_l2_logistic(
    X_std: np.ndarray,
    y: np.ndarray,
    l2: float = 25.0,
    max_iter: int = 35,
) -> Tuple[float, np.ndarray]:
    """Fit L2-regularized logistic regression via IRLS / Newton-Raphson."""
    n, d = X_std.shape
    Xb = np.column_stack([np.ones(n), X_std])
    w = np.zeros(d + 1, dtype=float)
    reg = np.diag([0.0] + [float(l2)] * d)

    for _ in range(max_iter):
        p = _sigmoid(Xb @ w)
        weights = np.clip(p * (1.0 - p), 1e-6, 0.25)
        grad = Xb.T @ (p - y) + reg @ w
        hess = (Xb.T * weights) @ Xb + reg + np.eye(d + 1) * 1e-6
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.max(np.abs(step)) < 1e-6:
            break
    return float(w[0]), w[1:]


def fit_l2_ridge(
    X_std: np.ndarray,
    y_ret: np.ndarray,
    l2: float = 50.0,
) -> Tuple[float, np.ndarray]:
    """Fit closed-form L2 Ridge regressor with unpenalized intercept."""
    n, d = X_std.shape
    Xb = np.column_stack([np.ones(n), X_std])
    reg = np.diag([0.0] + [float(l2)] * d)
    lhs = Xb.T @ Xb + reg + np.eye(d + 1) * 1e-8
    rhs = Xb.T @ y_ret
    w = np.linalg.solve(lhs, rhs)
    return float(w[0]), w[1:]


@dataclass(frozen=True)
class ModelEvalSummary:
    name: str
    total_bars: int
    trades: int
    accuracy_pct: float
    ci95_low_pct: float
    ci95_high_pct: float
    log_loss: Optional[float]
    brier: Optional[float]
    confusion: Dict[str, int]
    pnl_by_fee_bps: Dict[int, Dict[str, float]]


def _simulate_strategy(
    positions: np.ndarray,
    actual_rets: np.ndarray,
    fee_bps_list: Sequence[int] = (0, 4, 8, 10),
    initial_capital: float = 1000.0,
) -> Dict[int, Dict[str, float]]:
    """
    Simulate 15m directional PnL under round-trip transaction costs.

    Each entry into a +1/-1 position pays `fee_bps / 10_000` round-trip whenever trading.
    """
    out: Dict[int, Dict[str, float]] = {}
    traded = positions != 0
    for bps in fee_bps_list:
        cost = (bps / 10_000.0) * traded.astype(float)
        bar_net = positions * actual_rets - cost
        equity = initial_capital * np.cumprod(1.0 + bar_net)
        peak = np.maximum.accumulate(np.concatenate([[initial_capital], equity]))[1:]
        dd_pct = float(np.max((peak - equity) / peak) * 100.0) if len(equity) else 0.0
        final_eq = float(equity[-1]) if len(equity) else initial_capital
        ret_pct = (final_eq / initial_capital - 1.0) * 100.0
        win_rate = float(np.mean(bar_net[traded] > 0.0) * 100.0) if np.any(traded) else 0.0
        out[int(bps)] = {
            "final_equity": round(final_eq, 2),
            "return_pct": round(ret_pct, 2),
            "max_dd_pct": round(dd_pct, 2),
            "net_win_rate_pct": round(win_rate, 2),
        }
    return out


def _summarize_model(
    name: str,
    probs: Optional[np.ndarray],
    positions: np.ndarray,
    actual_up: np.ndarray,
    actual_rets: np.ndarray,
    fee_bps_list: Sequence[int] = (0, 4, 8, 10),
) -> ModelEvalSummary:
    traded_mask = positions != 0
    n_trades = int(np.sum(traded_mask))
    if n_trades > 0:
        pred_up = positions[traded_mask] > 0
        true_up = actual_up[traded_mask] > 0.5
        correct = (pred_up == true_up).astype(float)
        acc = float(np.mean(correct) * 100.0)
        # Bootstrap 95% confidence interval (deterministic seed)
        rng = np.random.default_rng(42)
        boots = rng.choice(correct, size=(800, n_trades), replace=True).mean(axis=1) * 100.0
        ci_lo, ci_hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
        tp = int(np.sum(pred_up & true_up))
        fp = int(np.sum(pred_up & ~true_up))
        tn = int(np.sum(~pred_up & ~true_up))
        fn = int(np.sum(~pred_up & true_up))
    else:
        acc, ci_lo, ci_hi = 0.0, 0.0, 0.0
        tp = fp = tn = fn = 0

    ll: Optional[float] = None
    brier: Optional[float] = None
    if probs is not None:
        pc = np.clip(probs, 1e-6, 1.0 - 1e-6)
        ll = round(float(-np.mean(actual_up * np.log(pc) + (1.0 - actual_up) * np.log(1.0 - pc))), 5)
        brier = round(float(np.mean((pc - actual_up) ** 2)), 5)

    return ModelEvalSummary(
        name=name,
        total_bars=len(actual_up),
        trades=n_trades,
        accuracy_pct=round(acc, 2),
        ci95_low_pct=round(ci_lo, 2),
        ci95_high_pct=round(ci_hi, 2),
        log_loss=ll,
        brier=brier,
        confusion={"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        pnl_by_fee_bps=_simulate_strategy(positions, actual_rets, fee_bps_list=fee_bps_list),
    )


def run_walk_forward_experiment(
    df_raw: pd.DataFrame,
    train_bars: int = 1000,
    refit_every: int = 96,
    l2_logistic: float = 25.0,
    l2_ridge: float = 50.0,
    interval: str = "15m",
) -> Dict[str, Any]:
    """
    Execute chronological walk-forward training and out-of-sample evaluation.

    Fits standardization parameters strictly on each historical training slice before scoring
    the immediately following out-of-sample window.
    """
    iv = interval if interval in ("5m", "15m") else "15m"
    iv_mins = 5 if iv == "5m" else 15
    bar_delta = pd.Timedelta(minutes=iv_mins)

    featured = build_causal_dataset(df_raw)
    live_row = featured.iloc[-1].copy()

    # Keep only rows with finite features AND finite next-bar target
    valid_mask = featured[list(FEATURE_COLUMNS)].notna().all(axis=1) & featured["next_ret"].notna()
    missing_rows = int((~valid_mask).sum())
    clean = featured.loc[valid_mask].reset_index(drop=True)

    if len(clean) < train_bars + 40:
        raise ValueError(f"Insufficient clean bars ({len(clean)}) for train_bars={train_bars}")

    X_all = clean[list(FEATURE_COLUMNS)].to_numpy(dtype=float)
    y_up_all = clean["target_up"].to_numpy(dtype=float)
    y_ret_all = clean["next_ret"].to_numpy(dtype=float)
    ret1_all = clean["ret_1"].to_numpy(dtype=float)

    n_total = len(clean)
    oos_start = train_bars
    oos_n = n_total - oos_start

    log_probs = np.zeros(oos_n, dtype=float)
    ridge_preds = np.zeros(oos_n, dtype=float)
    majority_pos = np.zeros(oos_n, dtype=float)

    last_weights = np.zeros(len(FEATURE_COLUMNS), dtype=float)
    for start in range(oos_start, n_total, refit_every):
        end = min(n_total, start + refit_every)
        tr_lo = max(0, start - train_bars)
        X_tr = X_all[tr_lo:start]
        y_tr_up = y_up_all[tr_lo:start]
        y_tr_ret = y_ret_all[tr_lo:start]

        # Standardize strictly inside training fold
        mean, std = fit_standardizer(X_tr)
        X_tr_std = (X_tr - mean) / std
        X_te_std = (X_all[start:end] - mean) / std

        b_log, w_log = fit_l2_logistic(X_tr_std, y_tr_up, l2=l2_logistic)
        b_reg, w_reg = fit_l2_ridge(X_tr_std, y_tr_ret, l2=l2_ridge)
        last_weights = w_log

        s_i, e_i = start - oos_start, end - oos_start
        log_probs[s_i:e_i] = _sigmoid(b_log + X_te_std @ w_log)
        ridge_preds[s_i:e_i] = b_reg + X_te_std @ w_reg
        majority_pos[s_i:e_i] = 1.0 if float(np.mean(y_tr_up)) >= 0.5 else -1.0

    y_oos_up = y_up_all[oos_start:]
    y_oos_ret = y_ret_all[oos_start:]
    ret1_oos = ret1_all[oos_start:]

    prev_rev_pos = np.where(ret1_oos > 0.0, -1.0, 1.0)
    log_pos_all = np.where(log_probs >= 0.5, 1.0, -1.0)
    log_pos_c55 = np.where(log_probs >= 0.55, 1.0, np.where(log_probs <= 0.45, -1.0, 0.0))
    log_pos_c58 = np.where(log_probs >= 0.58, 1.0, np.where(log_probs <= 0.42, -1.0, 0.0))
    ridge_pos_all = np.where(ridge_preds >= 0.0, 1.0, -1.0)

    models = [
        _summarize_model("Naive Majority Baseline", None, majority_pos, y_oos_up, y_oos_ret),
        _summarize_model(f"Prev-{iv} Mean Reversion", None, prev_rev_pos, y_oos_up, y_oos_ret),
        _summarize_model("Walk-Forward Ridge Return", None, ridge_pos_all, y_oos_up, y_oos_ret),
        _summarize_model("Walk-Forward Logistic (All Bars)", log_probs, log_pos_all, y_oos_up, y_oos_ret),
        _summarize_model("Logistic High-Conf (>=55% / <=45%)", log_probs, log_pos_c55, y_oos_up, y_oos_ret),
        _summarize_model("Logistic Ultra-Conf (>=58% / <=42%)", log_probs, log_pos_c58, y_oos_up, y_oos_ret),
    ]

    # Recent 8 completed predictions scored against actual closes (Phnom Penh UTC+7)
    local_offset = pd.Timedelta(hours=7)
    recent_checks: List[Dict[str, Any]] = []
    ts_oos = pd.to_datetime(clean["timestamp"].iloc[oos_start:], utc=True)
    close_oos = clean["close"].iloc[oos_start:].to_numpy(dtype=float)
    next_close_oos = clean["next_close"].iloc[oos_start:].to_numpy(dtype=float)
    tail_n = min(8, oos_n)
    for idx_i in range(oos_n - tail_n, oos_n):
        pred_open_local = ts_oos.iloc[idx_i] + bar_delta + local_offset
        pred_close_local = pred_open_local + bar_delta
        p_val = float(log_probs[idx_i])
        ret_val = float(y_oos_ret[idx_i])
        actual_dir = "UP" if ret_val > 0.0 else "DOWN"
        raw_call = "UP" if p_val >= 0.5 else "DOWN"
        if p_val >= 0.55:
            signal = "UP"
            verdict = "HIT" if actual_dir == "UP" else "MISS"
        elif p_val <= 0.45:
            signal = "DOWN"
            verdict = "HIT" if actual_dir == "DOWN" else "MISS"
        else:
            signal = "SKIP"
            verdict = f"SKIP ({'raw HIT' if raw_call == actual_dir else 'raw MISS'})"
        recent_checks.append(
            {
                "window_local": f"{pred_open_local:%m-%d %H:%M}->{pred_close_local:%H:%M}",
                "ref_close": round(float(close_oos[idx_i]), 2),
                "actual_close": round(float(next_close_oos[idx_i]), 2),
                "ret_pct": round(ret_val * 100.0, 3),
                "prob_up_pct": round(p_val * 100.0, 2),
                "signal": signal,
                "actual_dir": actual_dir,
                "verdict": verdict,
            }
        )

    # Live paper forecast on latest closed candle using final rolling window
    live_forecast: Optional[Dict[str, Any]] = None
    if not live_row[list(FEATURE_COLUMNS)].isna().any():
        X_last_tr = X_all[-train_bars:]
        y_last_tr = y_up_all[-train_bars:]
        mean, std = fit_standardizer(X_last_tr)
        b_log, w_log = fit_l2_logistic((X_last_tr - mean) / std, y_last_tr, l2=l2_logistic)
        x_now = (live_row[list(FEATURE_COLUMNS)].to_numpy(dtype=float) - mean) / std
        p_up = float(_sigmoid(np.array([b_log + float(x_now @ w_log)]))[0])
        last_weights = w_log
        base_open_utc = pd.Timestamp(live_row["timestamp"])
        target_open_utc = base_open_utc + bar_delta
        target_close_utc = target_open_utc + bar_delta
        target_open_local = target_open_utc + local_offset
        target_close_local = target_close_utc + local_offset
        live_forecast = {
            "interval": iv,
            "as_of_utc": str(live_row["timestamp"]),
            "target_window_local": f"{target_open_local:%Y-%m-%d %H:%M} -> {target_close_local:%H:%M} (UTC+7)",
            "target_open_epoch": int(target_open_utc.timestamp()),
            "target_close_epoch": int(target_close_utc.timestamp()),
            "close_price": round(float(live_row["close"]), 2),
            "prob_next_15m_up": round(p_up, 4),
            "prob_next_up_pct": round(p_up * 100.0, 2),
            "direction": "UP" if p_up >= 0.55 else ("DOWN" if p_up <= 0.45 else "SKIP"),
            "paper_signal": "UP (>=55%)" if p_up >= 0.55 else ("DOWN (<=45%)" if p_up <= 0.45 else f"SKIP / NO EDGE (raw lean: {'UP' if p_up >= 0.5 else 'DOWN'})"),
        }

    buy_hold_eq = float(1000.0 * np.prod(1.0 + y_oos_ret))
    top_features = sorted(
        [(col, round(float(wt), 4)) for col, wt in zip(FEATURE_COLUMNS, last_weights)],
        key=lambda item: abs(item[1]),
        reverse=True,
    )

    return {
        "interval": iv,
        "bars_raw": len(df_raw),
        "bars_dropped_warmup_or_nan": missing_rows,
        "oos_bars": oos_n,
        "oos_start_utc": str(clean["timestamp"].iloc[oos_start]),
        "oos_end_utc": str(clean["timestamp"].iloc[-1]),
        "avg_abs_15m_move_bps": round(float(np.mean(np.abs(y_oos_ret)) * 10_000.0), 2),
        "buy_and_hold_final_equity": round(buy_hold_eq, 2),
        "buy_and_hold_return_pct": round((buy_hold_eq / 1000.0 - 1.0) * 100.0, 2),
        "models": models,
        "top_features": top_features[:8],
        "recent_paper_checks": recent_checks,
        "live_paper_forecast": live_forecast,
        "oos_dates": clean["timestamp"].iloc[oos_start:].astype(str).tolist(),
        "oos_actual_rets": y_oos_ret,
        "oos_log_pos": log_pos_all,
        "oos_log_conf_pos": log_pos_c55,
    }


class BtcResearchService:
    """Thread-safe, TTL-cached paper BTC 5m and 15m prediction service for the web terminal."""

    def __init__(
        self,
        ttl_seconds: float = 45.0,
        bars_per_horizon: int = 3500,
        train_bars: int = 1000,
        refit_every: int = 96,
        fetcher: Callable[..., Tuple[pd.DataFrame, str]] = fetch_btc_15m_candles,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.bars_per_horizon = bars_per_horizon
        self.train_bars = train_bars
        self.refit_every = refit_every
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._cache: Optional[Tuple[float, Dict[str, Any]]] = None

    def _compute_horizon(self, interval: str) -> Dict[str, Any]:
        df_raw, source = self._fetcher(total_bars=self.bars_per_horizon, symbol="BTCUSDT", interval=interval)
        res = run_walk_forward_experiment(
            df_raw,
            train_bars=self.train_bars,
            refit_every=self.refit_every,
            interval=interval,
        )
        by_name = {m.name: m for m in res["models"]}
        all_m = by_name["Walk-Forward Logistic (All Bars)"]
        conf_m = by_name["Logistic High-Conf (>=55% / <=45%)"]
        ultra_m = by_name["Logistic Ultra-Conf (>=58% / <=42%)"]
        hits = sum(1 for c in res["recent_paper_checks"] if c["verdict"] == "HIT")
        misses = sum(1 for c in res["recent_paper_checks"] if c["verdict"] == "MISS")
        return {
            "interval": interval,
            "source": source,
            "live": res["live_paper_forecast"],
            "recent_checks": res["recent_paper_checks"],
            "recent_hits": hits,
            "recent_misses": misses,
            "oos_summary": {
                "oos_bars": res["oos_bars"],
                "avg_abs_move_bps": res["avg_abs_15m_move_bps"],
                "all_acc_pct": all_m.accuracy_pct,
                "all_ci95": f"[{all_m.ci95_low_pct:.1f}%, {all_m.ci95_high_pct:.1f}%]",
                "conf_acc_pct": conf_m.accuracy_pct,
                "conf_trades": conf_m.trades,
                "ultra_acc_pct": ultra_m.accuracy_pct,
                "ultra_trades": ultra_m.trades,
                "gross_0bps_pct": all_m.pnl_by_fee_bps[0]["return_pct"],
                "net_4bps_pct": all_m.pnl_by_fee_bps[4]["return_pct"],
                "net_8bps_pct": all_m.pnl_by_fee_bps[8]["return_pct"],
                "conf_8bps_pct": conf_m.pnl_by_fee_bps[8]["return_pct"],
                "ultra_8bps_pct": ultra_m.pnl_by_fee_bps[8]["return_pct"],
            },
            "top_features": [{"name": k, "weight": v} for k, v in res["top_features"][:5]],
        }

    def get_predictions(
        self,
        intervals: Sequence[str] = ("5m", "15m"),
        force: bool = False,
    ) -> Dict[str, Any]:
        now_mono = time.monotonic()
        with self._lock:
            if not force and self._cache is not None and (now_mono - self._cache[0]) < self.ttl_seconds:
                return self._cache[1]

        valid_ivs = [iv for iv in intervals if iv in ("5m", "15m")] or ["5m", "15m"]
        horizons: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=len(valid_ivs)) as pool:
            fut_map = {iv: pool.submit(self._compute_horizon, iv) for iv in valid_ivs}
            for iv, fut in fut_map.items():
                try:
                    horizons[iv] = fut.result()
                except Exception as exc:
                    horizons[iv] = {"interval": iv, "error": str(exc), "recent_checks": []}

        payload: Dict[str, Any] = {
            "symbol": "BTCUSDT.P",
            "paper_only": True,
            "updated_at_epoch": int(time.time()),
            "horizons": horizons,
        }
        with self._lock:
            self._cache = (time.monotonic(), payload)
        return payload


def generate_experiment_html(result: Dict[str, Any], out_path: Path | str = "btc_15m_report.html") -> Path:
    """Write interactive Plotly paper comparison chart (`0 bps` gross vs `8 bps` taker costs)."""
    import plotly.graph_objects as go

    iv = result.get("interval", "15m")
    dates = result["oos_dates"]
    rets = result["oos_actual_rets"]
    pos_all = result["oos_log_pos"]
    pos_conf = result["oos_log_conf_pos"]

    bh = 1000.0 * np.cumprod(1.0 + rets)
    eq_zero = 1000.0 * np.cumprod(1.0 + pos_all * rets)
    eq_taker = 1000.0 * np.cumprod(1.0 + pos_all * rets - 0.0008)
    eq_conf_taker = 1000.0 * np.cumprod(1.0 + pos_conf * rets - 0.0008 * (pos_conf != 0))

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=bh, name="BTC Buy & Hold ($1,000)", line={"color": "#94a3b8"}))
    fig.add_trace(go.Scatter(x=dates, y=eq_zero, name=f"{iv} ML All Bars (0 bps No Fees)", line={"color": "#22c55e"}))
    fig.add_trace(go.Scatter(x=dates, y=eq_taker, name=f"{iv} ML All Bars (8 bps Taker Fee)", line={"color": "#ef4444"}))
    fig.add_trace(go.Scatter(x=dates, y=eq_conf_taker, name=f"{iv} ML Conf >=55% (8 bps Taker Fee)", line={"color": "#f59e0b"}))
    fig.update_layout(
        template="plotly_dark",
        title=f"Paper-Only {iv} BTC Walk-Forward Experiment: Signal Edge vs. Trading Fees",
        yaxis_title="Paper Equity ($)",
        hovermode="x unified",
    )
    path = Path(out_path)
    fig.write_html(str(path), include_plotlyjs="cdn")
    return path


def format_live_scorecard(result: Dict[str, Any], source: str) -> str:
    iv = result.get("interval", "15m")
    lines: List[str] = [
        f"=== Live Paper {iv} BTC Scorecard ({source}) ===",
        f"{'Target Window (UTC+7)':20s} {'Start$':>10s} {'Close$':>10s} {'Actual':>13s} {'P(Up)':>7s} {'Call':>6s} {'Result':16s}",
        "-" * 88,
    ]
    for r in result.get("recent_paper_checks", []):
        actual_str = f"{r['actual_dir']:4s} {r['ret_pct']:+6.3f}%"
        lines.append(
            f"{r['window_local']:20s} {r['ref_close']:10,.2f} {r['actual_close']:10,.2f} "
            f"{actual_str:>13s} {r['prob_up_pct']:6.2f}% {r['signal']:>6s} {r['verdict']:16s}"
        )
    lf = result.get("live_paper_forecast")
    if lf:
        lines.append("-" * 88)
        lines.append(
            f"NEXT {iv} ({lf['target_window_local']}) vs ${lf['close_price']:,.2f}: "
            f"P(up) = {lf['prob_next_15m_up']*100:.2f}% -> {lf['paper_signal']} [PAPER ONLY]"
        )
    return "\n".join(lines)


def format_terminal_report(result: Dict[str, Any], source: str) -> str:
    iv = result.get("interval", "15m")
    lines: List[str] = [
        f"=== Paper-Only {iv} BTC Walk-Forward Research ({source}) ===",
        f"OOS Window : {result['oos_start_utc']} -> {result['oos_end_utc']} ({result['oos_bars']} x {iv} bars)",
        f"Warmup/NaN : {result['bars_dropped_warmup_or_nan']} rows excluded | Avg |{iv} move|: {result['avg_abs_15m_move_bps']} bps",
        f"Buy & Hold : ${result['buy_and_hold_final_equity']:.2f} ({result['buy_and_hold_return_pct']:+.2f}% on $1,000)",
        "",
        f"{'Model':36s} {'Trades':>7s} {'Acc%':>7s} {'95% CI':>15s} {'0bps':>9s} {'4bps':>9s} {'8bps':>9s} {'10bps':>9s}",
        "-" * 108,
    ]
    for m in result["models"]:
        p = m.pnl_by_fee_bps
        ci = f"[{m.ci95_low_pct:4.1f},{m.ci95_high_pct:4.1f}]"
        lines.append(
            f"{m.name:36s} {m.trades:7d} {m.accuracy_pct:6.2f}% {ci:>15s} "
            f"{p[0]['return_pct']:+8.2f}% {p[4]['return_pct']:+8.2f}% {p[8]['return_pct']:+8.2f}% {p[10]['return_pct']:+8.2f}%"
        )

    lines.append("")
    lines.append("Top learned logistic weights (standardized): " + ", ".join(f"{k}={v:+.3f}" for k, v in result["top_features"]))
    lines.append("")
    lines.append(format_live_scorecard(result, source))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-only 5m / 15m BTC directional prediction & cost study")
    parser.add_argument("--interval", choices=("5m", "15m"), default="15m", help="Candle timeframe (5m or 15m)")
    parser.add_argument("--bars", type=int, default=3000, help="Total candles to fetch (max 6000)")
    parser.add_argument("--train-bars", type=int, default=1000, help="Rolling training window length in bars")
    parser.add_argument("--refit-every", type=int, default=96, help="Refit cadence in bars")
    parser.add_argument("--html-out", default="btc_15m_report.html", help="Path for interactive Plotly equity chart")
    parser.add_argument("--now", action="store_true", help="Print only recent paper results + current next-bar forecast")
    args = parser.parse_args()

    fetch_bars = min(args.bars, args.train_bars + 300) if args.now else args.bars
    df_raw, source = fetch_btc_15m_candles(total_bars=fetch_bars, interval=args.interval)
    result = run_walk_forward_experiment(df_raw, train_bars=args.train_bars, refit_every=args.refit_every, interval=args.interval)
    if args.now:
        print(format_live_scorecard(result, source))
        return

    report_path = generate_experiment_html(result, out_path=args.html_out)
    print(format_terminal_report(result, source))
    print(f"\nSaved interactive chart: {report_path}")


if __name__ == "__main__":
    main()
