"""Unit and statistical invariant tests for paper-only 15m BTC research."""

from __future__ import annotations

import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btc_15m_research import (
    FEATURE_COLUMNS,
    BtcResearchService,
    build_causal_dataset,
    fetch_btc_15m_candles,
    run_walk_forward_experiment,
)


def _synthetic_15m_df(n: int = 200, seed: int = 7, interval_minutes: int = 15) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start_ms = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    bar_ms = interval_minutes * 60 * 1000

    closes = [60000.0]
    taker_ratios = rng.uniform(0.35, 0.65, size=n)
    for i in range(1, n):
        signal = 0.0020 * (taker_ratios[i - 1] - 0.5) * 4.0
        noise = float(rng.normal(0.0, 0.0002))
        closes.append(closes[-1] * (1.0 + signal + noise))

    rows = []
    for i in range(n):
        c = closes[i]
        o = closes[i - 1] if i > 0 else c
        hi = max(o, c) * 1.0008
        lo = min(o, c) * 0.9992
        vol = float(rng.uniform(50.0, 500.0))
        rows.append(
            {
                "open_time_ms": start_ms + i * bar_ms,
                "timestamp": pd.Timestamp(start_ms + i * bar_ms, unit="ms", tz="UTC"),
                "open": o,
                "high": hi,
                "low": lo,
                "close": c,
                "volume": vol,
                "quote_volume": vol * c,
                "trades": int(vol * 10),
                "taker_buy_vol": vol * float(taker_ratios[i]),
            }
        )
    return pd.DataFrame(rows)


def test_causal_features_no_future_leakage() -> None:
    df = _synthetic_15m_df(n=150)
    base = build_causal_dataset(df)

    mutated = df.copy()
    mutated.iloc[-10:, mutated.columns.get_loc("close")] *= 3.5
    mutated.iloc[-10:, mutated.columns.get_loc("high")] *= 4.0
    mutated.iloc[-10:, mutated.columns.get_loc("volume")] *= 50.0
    after = build_causal_dataset(mutated)

    for col in FEATURE_COLUMNS:
        np.testing.assert_allclose(
            base[col].iloc[:-10].to_numpy(dtype=float),
            after[col].iloc[:-10].to_numpy(dtype=float),
            equal_nan=True,
        )


def test_walk_forward_recovers_planted_signal_and_tracks_fees() -> None:
    df = _synthetic_15m_df(n=1400, seed=11)
    res = run_walk_forward_experiment(df, train_bars=500, refit_every=100)
    by_name = {m.name: m for m in res["models"]}

    logit = by_name["Walk-Forward Logistic (All Bars)"]
    assert logit.accuracy_pct >= 72.0
    assert logit.pnl_by_fee_bps[0]["return_pct"] > 0.0
    assert logit.pnl_by_fee_bps[10]["return_pct"] < logit.pnl_by_fee_bps[0]["return_pct"]
    assert res["live_paper_forecast"] is not None
    assert "UTC+7" in res["live_paper_forecast"]["target_window_local"]
    assert len(res["recent_paper_checks"]) == 8
    assert all(c["verdict"] for c in res["recent_paper_checks"])


def test_zero_volume_and_flat_candles_remain_finite() -> None:
    df = _synthetic_15m_df(n=120, seed=3)
    df.loc[60:65, "volume"] = 0.0
    df.loc[60:65, "taker_buy_vol"] = 0.0
    df.loc[60:65, "high"] = df.loc[60:65, "close"]
    df.loc[60:65, "low"] = df.loc[60:65, "close"]

    out = build_causal_dataset(df)
    tail = out.loc[60:, list(FEATURE_COLUMNS)].to_numpy(dtype=float)
    assert np.isfinite(tail).all()


def test_fetch_btc_15m_candles_falls_back_to_hyperliquid() -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    bar_ms = 15 * 60 * 1000
    base_ms = now_ms - 450 * bar_ms

    def fail_binance(url: str):
        raise urllib.error.HTTPError(url, 451, "Unavailable For Legal Reasons", {}, None)

    def fake_hl(url: str, payload):
        assert payload["req"]["coin"] == "BTC"
        return [
            {
                "t": base_ms + i * bar_ms,
                "o": "60000",
                "h": "60100",
                "l": "59900",
                "c": "60050",
                "v": "100",
                "n": 250,
            }
            for i in range(420)
        ]

    df, src = fetch_btc_15m_candles(total_bars=400, http_getter=fail_binance, http_poster=fake_hl)
    assert "Hyperliquid" in src
    assert len(df) >= 390
    assert (df["open_time_ms"].diff().dropna() > 0).all()


def test_btc_research_service_returns_5m_and_15m_horizons_and_caches() -> None:
    calls = []

    def fake_fetcher(total_bars: int = 1350, symbol: str = "BTCUSDT", interval: str = "15m"):
        calls.append(interval)
        mins = 5 if interval == "5m" else 15
        return _synthetic_15m_df(n=1200, seed=19 if mins == 5 else 23, interval_minutes=mins), f"Mock ({interval})"

    svc = BtcResearchService(ttl_seconds=60.0, bars_per_horizon=1200, train_bars=700, refit_every=72, fetcher=fake_fetcher)
    p1 = svc.get_predictions()
    assert p1["paper_only"] is True
    assert set(p1["horizons"].keys()) == {"5m", "15m"}
    assert p1["horizons"]["5m"]["live"]["interval"] == "5m"
    assert p1["horizons"]["15m"]["live"]["interval"] == "15m"
    assert len(p1["horizons"]["5m"]["recent_checks"]) == 8
    assert len(p1["horizons"]["15m"]["recent_checks"]) == 8
    assert len(calls) == 2

    # Second call within TTL uses cache
    p2 = svc.get_predictions()
    assert p2["updated_at_epoch"] == p1["updated_at_epoch"]
    assert len(calls) == 2

