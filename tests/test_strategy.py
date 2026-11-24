"""Unit tests for strategy rules, CISD body-close, news filtering, and backtester."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

import live_mt5_bot
from backtester import Backtester
from config import StrategyConfig
from data_loader import resample_timeframes
from indicators.aoi import detect_fvgs, detect_order_blocks, get_active_1h_aois
from indicators.bias import determine_daily_bias, is_ranging_day
from indicators.cisd import detect_5m_cisd
from live_mt5_bot import (
    MAGIC_NUMBER,
    _closed_1h_bars,
    _pick_filling_mode,
    _place_market_order,
    _trail_open_positions,
    compute_lot_size,
    effective_tick_value,
    evaluate_donchian_1h_signal,
    is_trading_window,
    prepare_live_donchian_frames,
    replay_recent_bars,
)
from models import Bias, NewsEvent, TradeStatus
from news_calendar import NewsCalendar
from smc_bot import evaluate_15m_smc_signal

NY_TZ = ZoneInfo("America/New_York")


def _make_df(rows, start="2026-10-05 00:00", freq="5min"):
    idx = pd.date_range(start=start, periods=len(rows), freq=freq, tz=NY_TZ)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def test_daily_bias_green_red_and_doji():
    # Day 1: Green, Day 2: Red, Day 3: Doji
    daily = _make_df(
        [
            [1.1000, 1.1100, 1.0950, 1.1080],
            [1.1080, 1.1100, 1.0900, 1.0920],
            [1.0920, 1.1000, 1.0850, 1.0921],  # Doji (body ~0.0001 / range 0.015)
        ],
        start="2026-10-05 17:00",
        freq="1D",
    )
    # Build 1H uptrend for Day 3
    h_rows = []
    price = 1.0850
    for i in range(48):
        o = price
        c = price + (0.0008 if i % 3 != 0 else -0.0003)
        h_rows.append([o, max(o, c) + 0.0002, min(o, c) - 0.0002, c])
        price = c
    hourly = _make_df(h_rows, start="2026-10-06 17:00", freq="1h")

    assert determine_daily_bias(daily, hourly, pd.Timestamp("2026-10-06 18:00", tz=NY_TZ)) == Bias.BULLISH
    assert determine_daily_bias(daily, hourly, pd.Timestamp("2026-10-07 18:00", tz=NY_TZ)) == Bias.BEARISH
    assert determine_daily_bias(daily, hourly, pd.Timestamp("2026-10-08 18:00", tz=NY_TZ)) == Bias.BULLISH


def test_ranging_day_unbroken_2_3_days():
    # Prior 2 days: high=1.1200, low=1.0800; Last 24h strictly inside [1.0900, 1.1100]
    rows = [[1.10, 1.12, 1.08, 1.10] for _ in range(48)] + [
        [1.10, 1.11, 1.09, 1.10] for _ in range(24)
    ]
    hourly = _make_df(rows, start="2026-10-05 00:00", freq="1h")
    assert is_ranging_day(hourly, pd.Timestamp("2026-10-08 00:00", tz=NY_TZ), lookback_days=3) is True

    # Break high in last 24h -> no longer ranging
    hourly.iloc[-1, hourly.columns.get_loc("high")] = 1.13
    assert is_ranging_day(hourly, pd.Timestamp("2026-10-08 00:00", tz=NY_TZ), lookback_days=3) is False


def test_1h_fvg_and_ob_lookback():
    # 1H candles: Bullish FVG between bar 0 high (1.1000) and bar 2 low (1.1020)
    rows = [[1.095, 1.100, 1.094, 1.099]] * 24
    # Put an unmitigated bullish FVG in the last 24h
    rows[-3] = [1.098, 1.100, 1.097, 1.099]
    rows[-2] = [1.099, 1.103, 1.099, 1.1025]
    rows[-1] = [1.1025, 1.105, 1.102, 1.104]
    hourly = _make_df(rows, start="2026-10-07 00:00", freq="1h")

    fvgs = detect_fvgs(hourly, "1H")
    assert any(f.direction == Bias.BULLISH and not f.mitigated for f in fvgs)

    aois = get_active_1h_aois(hourly, pd.Timestamp("2026-10-08 00:00", tz=NY_TZ), Bias.BULLISH)
    assert len(aois) >= 1


def test_5m_cisd_requires_body_close_not_wick():
    # Prior up-close candles before the bearish OB (down-close candle with body top = 1.1010)
    base = [[1.1015, 1.1025, 1.1014, 1.1020] for _ in range(10)]
    base.append([1.1010, 1.1025, 1.0995, 1.1000])  # down-close OB candle (body top = 1.1010)
    # Next candle wicks above 1.1010 (high=1.1020) but closes BELOW 1.1010 -> NOT CISD
    wick_only = base + [[1.1000, 1.1020, 1.0998, 1.1008]]
    df_wick = _make_df(wick_only, freq="5min")
    assert detect_5m_cisd(df_wick, Bias.BULLISH) is None

    # Next candle body-closes STRICTLY above 1.1010 -> CISD confirmed, limit at 1.1010
    body_close = wick_only + [[1.1008, 1.1022, 1.1005, 1.1015]]
    df_body = _make_df(body_close, freq="5min")
    sig = detect_5m_cisd(df_body, Bias.BULLISH)
    assert sig is not None
    assert sig.ob_body_level == 1.1010
    assert sig.sl_price < sig.ob_body_level


def test_monday_and_news_rules():
    cfg = StrategyConfig()
    # Monday 2026-10-12 skipped completely when skip_monday=True
    cal_none = NewsCalendar()
    mon_london = datetime(2026, 10, 12, 3, 0, tzinfo=NY_TZ)
    mon_ny = datetime(2026, 10, 12, 8, 30, tzinfo=NY_TZ)
    assert cal_none.check_eligibility(mon_london, "EURUSD", cfg)[0] is False
    assert cal_none.check_eligibility(mon_ny, "EURUSD", cfg)[0] is False

    # Tuesday with USD CPI at 08:30 -> [07:30, 09:30] allowed, no +1h force close
    cal_usd = NewsCalendar([NewsEvent(datetime(2026, 10, 13, 8, 30, tzinfo=NY_TZ), "USD", "CPI")])
    ok_news, sess_n, _, is_n, close_at = cal_usd.check_eligibility(datetime(2026, 10, 13, 7, 45, tzinfo=NY_TZ), "EURUSD", cfg)
    assert ok_news is True and sess_n == "USD_NEWS" and is_n is True and close_at is None
    assert cal_usd.check_eligibility(datetime(2026, 10, 13, 10, 0, tzinfo=NY_TZ), "EURUSD", cfg)[0] is False

    # Tuesday with AUD news -> ny_only=True means Asia skipped, NY allowed
    cal_aud = NewsCalendar([NewsEvent(datetime(2026, 10, 13, 19, 30, tzinfo=NY_TZ), "AUD", "Jobs")])
    assert cal_aud.check_eligibility(datetime(2026, 10, 13, 19, 0, tzinfo=NY_TZ), "AUDUSD", cfg)[0] is False
    assert cal_aud.check_eligibility(datetime(2026, 10, 13, 3, 0, tzinfo=NY_TZ), "GBPUSD", cfg)[0] is False
    assert cal_aud.check_eligibility(datetime(2026, 10, 13, 8, 30, tzinfo=NY_TZ), "EURUSD", cfg)[0] is True

    # Year-end holiday blackout (last 5 days: Dec 27-31)
    assert cal_none.check_eligibility(datetime(2025, 12, 26, 8, 30, tzinfo=NY_TZ), "EURUSD", cfg)[0] is True
    assert cal_none.check_eligibility(datetime(2025, 12, 29, 8, 30, tzinfo=NY_TZ), "EURUSD", cfg)[0] is False
    assert cal_none.check_eligibility(datetime(2025, 12, 31, 8, 30, tzinfo=NY_TZ), "EURUSD", cfg)[0] is False


def test_backtester_smoke_run():
    # Generate 6 days of synthetic 5m data and verify backtester runs end-to-end
    rng = np.random.default_rng(42)
    n = 6 * 24 * 12  # 6 days of 5m bars
    idx = pd.date_range("2026-10-05 00:00", periods=n, freq="5min", tz=NY_TZ)
    returns = rng.normal(0, 0.0005, size=n)
    close = 1.1000 + np.cumsum(returns)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + rng.uniform(0.0001, 0.0006, size=n)
    low = np.minimum(open_, close) - rng.uniform(0.0001, 0.0006, size=n)
    df_5m = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)

    data = {"EURUSD": resample_timeframes(df_5m)}
    bt = Backtester(data=data, cfg=StrategyConfig(symbols=["EURUSD"]))
    trades = bt.run()
    assert isinstance(trades, list)
    assert len(bt.equity_curve) > 0


def test_live_bot_lot_size_window_and_donchian_signal():
    # Lot sizing: $1,000 risk, 10.0 price SL, tick_size=0.01, tick_value=1.0 -> 1.00 lot
    lots = compute_lot_size(
        risk_amount=1000.0,
        sl_distance=10.0,
        tick_size=0.01,
        tick_value=1.0,
        vol_min=0.01,
        vol_max=100.0,
        vol_step=0.01,
    )
    assert lots == 1.00
    assert compute_lot_size(5.0, 100.0, 0.01, 1.0, 0.01, 100.0, 0.01) == 0.0

    # Trading window: 24/5 Mon-Fri before Fri 16:00 NY; Sat, Fri >= 16:00, Dec 29 blocked
    assert is_trading_window(datetime(2026, 10, 13, 17, 0, tzinfo=NY_TZ)) is True
    assert is_trading_window(datetime(2026, 10, 16, 15, 0, tzinfo=NY_TZ)) is True
    assert is_trading_window(datetime(2026, 10, 16, 16, 0, tzinfo=NY_TZ)) is False
    assert is_trading_window(datetime(2026, 10, 17, 10, 0, tzinfo=NY_TZ)) is False
    assert is_trading_window(datetime(2026, 12, 29, 10, 0, tzinfo=NY_TZ)) is False

    # Filling mode selection matches symbol bitmask (USTEC=2 IOC, XAUUSD=3 IOC, FOK=1, fallback RETURN)
    assert _pick_filling_mode(SimpleNamespace(filling_mode=2)) == live_mt5_bot.mt5.ORDER_FILLING_IOC
    assert _pick_filling_mode(SimpleNamespace(filling_mode=3)) == live_mt5_bot.mt5.ORDER_FILLING_IOC
    assert _pick_filling_mode(SimpleNamespace(filling_mode=1)) == live_mt5_bot.mt5.ORDER_FILLING_FOK
    assert _pick_filling_mode(SimpleNamespace(filling_mode=0)) == live_mt5_bot.mt5.ORDER_FILLING_RETURN

    # 1H Donchian bullish breakout aligned with 1H EMA50/200 and 4H EMA20/50
    h1_rows = [[2000 + i * 1.5, 2003 + i * 1.5, 1998 + i * 1.5, 2001 + i * 1.5] for i in range(250)]
    # Flatten last 21 bars into a tight range [2370, 2380], then break out cleanly on final bar
    for k in range(229, 249):
        h1_rows[k] = [2375.0, 2380.0, 2370.0, 2376.0]
    h1_rows[249] = [2376.0, 2390.0, 2375.0, 2388.0]  # close > prior 20-bar high 2380
    raw_h1 = _make_df(h1_rows, start="2026-10-01 00:00", freq="1h")
    df1h, df4h = prepare_live_donchian_frames(raw_h1, donchian_period=20)
    sig = evaluate_donchian_1h_signal(df1h, df4h, sl_atr_mult=2.0)
    assert sig is not None
    direction, ref_close, sl_price, atr = sig
    assert direction == Bias.BULLISH
    assert ref_close == 2388.0
    assert sl_price < ref_close and atr > 0

    # Keep verifying smc_bot helper stays intact too
    ltf_rows = [[2440, 2442, 2438, 2441] for _ in range(13)] + [[2439, 2441, 2434, 2436] for _ in range(9)] + [
        [2436, 2440, 2435, 2439],
        [2439, 2444, 2439, 2443],
        [2443, 2450, 2442, 2448],
    ]
    df15 = _make_df(ltf_rows, start="2026-10-10 08:00", freq="15min")
    assert evaluate_15m_smc_signal(df15, df1h, df15.index[-1]) is not None


def test_place_market_order_payload_spread_guard_and_dry_run(monkeypatch):
    sent_requests = []
    info = SimpleNamespace(
        digits=2,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        point=0.01,
        filling_mode=2,  # IOC (matches live USTEC)
    )
    tick = SimpleNamespace(bid=2450.0, ask=2450.5)  # 0.50 spread -> $50 cost on 1.00 lot

    monkeypatch.setattr(live_mt5_bot.mt5, "symbol_info", lambda _sym: info)
    monkeypatch.setattr(live_mt5_bot.mt5, "symbol_info_tick", lambda _sym: tick)
    monkeypatch.setattr(
        live_mt5_bot.mt5,
        "order_send",
        lambda req: sent_requests.append(req) or SimpleNamespace(retcode=live_mt5_bot.mt5.TRADE_RETCODE_DONE, order=9991, price=2450.5),
    )

    # 1. Dry-run sends no order
    _place_market_order("USTEC", Bias.BULLISH, 10.0, 1000.0, 0.20, dry_run=True)
    assert sent_requests == []

    # 2. Spread guard rejects when spread cost ($50 on 1.00 lot) > 2% of $1,000 ($20)
    _place_market_order("USTEC", Bias.BULLISH, 10.0, 1000.0, 0.02, dry_run=False)
    assert sent_requests == []

    # 3. Live market order within spread cap sends exact DEAL + IOC payload
    _place_market_order("USTEC", Bias.BULLISH, 10.0, 1000.0, 0.20, dry_run=False)
    assert len(sent_requests) == 1
    req = sent_requests[0]
    assert req["action"] == live_mt5_bot.mt5.TRADE_ACTION_DEAL
    assert req["type"] == live_mt5_bot.mt5.ORDER_TYPE_BUY
    assert req["type_filling"] == live_mt5_bot.mt5.ORDER_FILLING_IOC
    assert req["symbol"] == "USTEC"
    assert req["volume"] == 1.00
    assert req["price"] == 2450.5
    assert req["sl"] == 2440.5
    assert req["tp"] == 0.0
    assert req["magic"] == MAGIC_NUMBER


def test_place_market_order_uses_order_calc_profit_over_mismatched_tick_value(monkeypatch):
    # Reproduces MetaQuotes-Demo XAUUSD reporting trade_tick_value=0.1 while true PnL is $100/pt per lot
    sent_requests = []
    info = SimpleNamespace(
        digits=2,
        trade_tick_size=0.01,
        trade_tick_value=0.1,
        trade_tick_value_loss=0.1,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        point=0.01,
        filling_mode=3,
    )
    tick = SimpleNamespace(bid=3800.0, ask=3800.2)

    def fake_calc_profit(order_type, symbol, volume, price_open, price_close):
        diff = (price_close - price_open) if order_type == live_mt5_bot.mt5.ORDER_TYPE_BUY else (price_open - price_close)
        return round(diff * 100.0 * volume, 2)

    monkeypatch.setattr(live_mt5_bot.mt5, "symbol_info", lambda _sym: info)
    monkeypatch.setattr(live_mt5_bot.mt5, "symbol_info_tick", lambda _sym: tick)
    monkeypatch.setattr(live_mt5_bot.mt5, "order_calc_profit", fake_calc_profit)
    monkeypatch.setattr(
        live_mt5_bot.mt5,
        "order_send",
        lambda req: sent_requests.append(req) or SimpleNamespace(retcode=live_mt5_bot.mt5.TRADE_RETCODE_DONE, order=9992, price=3800.0),
    )

    assert effective_tick_value("XAUUSD", info, 3800.0) == 1.0
    _place_market_order("XAUUSD", Bias.BEARISH, 40.0, 1000.0, 0.20, dry_run=False)
    assert len(sent_requests) == 1
    # True 1% ($1,000) risk at 40.0 SL ($4,000/lot) must size 0.25 lots, NOT 2.50 lots
    assert sent_requests[0]["volume"] == 0.25


def test_trail_open_positions_ratchets_and_isolates_magic(monkeypatch):
    # Closed bar at 2026-10-13 10:00 NY (closes 11:00 NY) -> NY+7h server epoch for entry times
    idx = pd.date_range("2026-10-13 08:00", periods=3, freq="1h", tz=NY_TZ)
    df1h = pd.DataFrame(
        {"open": [2440.0, 2450.0, 2460.0], "high": [2452.0, 2462.0, 2472.0], "low": [2438.0, 2448.0, 2458.0], "close": [2450.0, 2460.0, 2470.0], "atr14": [4.0, 4.0, 4.0]},
        index=idx,
    )
    # Broker timestamps are NY+7h naive epochs
    old_bot_epoch = int((datetime(2026, 10, 13, 9, 5, tzinfo=NY_TZ).replace(tzinfo=None) + timedelta(hours=7)).replace(tzinfo=timezone.utc).timestamp())
    same_bar_bot_epoch = int((datetime(2026, 10, 13, 11, 0, 5, tzinfo=NY_TZ).replace(tzinfo=None) + timedelta(hours=7)).replace(tzinfo=timezone.utc).timestamp())
    manual_epoch = old_bot_epoch

    buy_t = getattr(live_mt5_bot.mt5, "POSITION_TYPE_BUY", 0)
    positions = [
        SimpleNamespace(ticket=201, symbol="XAUUSD", magic=MAGIC_NUMBER, type=buy_t, sl=2445.0, tp=0.0, time=old_bot_epoch),
        SimpleNamespace(ticket=202, symbol="XAUUSD", magic=MAGIC_NUMBER, type=buy_t, sl=2455.0, tp=0.0, time=same_bar_bot_epoch),
        SimpleNamespace(ticket=203, symbol="XAUUSD", magic=999999, type=buy_t, sl=2400.0, tp=0.0, time=manual_epoch),
    ]
    info = SimpleNamespace(digits=2, trade_stops_level=0, point=0.01)
    tick = SimpleNamespace(bid=2469.0, ask=2469.5)
    updates = []

    monkeypatch.setattr(live_mt5_bot.mt5, "positions_get", lambda symbol=None: positions)
    monkeypatch.setattr(live_mt5_bot.mt5, "symbol_info", lambda _sym: info)
    monkeypatch.setattr(live_mt5_bot.mt5, "symbol_info_tick", lambda _sym: tick)
    monkeypatch.setattr(
        live_mt5_bot.mt5,
        "order_send",
        lambda req: updates.append(req) or SimpleNamespace(retcode=live_mt5_bot.mt5.TRADE_RETCODE_DONE),
    )

    last_trailed = {}
    _trail_open_positions("XAUUSD", df1h, trail_atr_mult=2.5, server_offset_hours=7, dry_run=False, last_trailed_bar=last_trailed)
    # 2470 - 2.5*4.0 = 2460.0 > 2445.0 -> updates ticket 201 only (202 opened after bar close; 203 is manual)
    assert len(updates) == 1
    assert updates[0]["action"] == live_mt5_bot.mt5.TRADE_ACTION_SLTP
    assert updates[0]["position"] == 201
    assert updates[0]["sl"] == 2460.0

    # Calling again on the same bar does not resend
    _trail_open_positions("XAUUSD", df1h, trail_atr_mult=2.5, server_offset_hours=7, dry_run=False, last_trailed_bar=last_trailed)
    assert len(updates) == 1


def test_replay_recent_bars_finds_donchian_signal_offline(monkeypatch):
    h1_rows = [[2000 + i * 1.5, 2003 + i * 1.5, 1998 + i * 1.5, 2001 + i * 1.5] for i in range(260)]
    for k in range(238, 258):
        h1_rows[k] = [2388.0, 2394.0, 2384.0, 2390.0]
    h1_rows[258] = [2390.0, 2406.0, 2389.0, 2404.0]  # breakout on closed bar (Thu Oct 15 18:00 NY)
    h1_rows[259] = [2404.0, 2408.0, 2402.0, 2406.0]  # future/forming bar relative to 19:30 NY
    df1h = _make_df(h1_rows, start="2026-10-05 00:00", freq="1h")

    # Encode as MT5 NY+7h epochs
    rates_1h = [
        {
            "time": int((ts.tz_localize(None) + timedelta(hours=7)).replace(tzinfo=timezone.utc).timestamp()),
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
        }
        for ts, r in df1h.iterrows()
    ]
    closed = _closed_1h_bars(rates_1h, now_ny=datetime(2026, 10, 15, 19, 30, tzinfo=NY_TZ), server_offset_hours=7)
    assert len(closed) == 259  # final forming bar excluded

    monkeypatch.setattr(live_mt5_bot.mt5, "copy_rates_from_pos", lambda _sym, _tf, _pos, _count: rates_1h)
    info = SimpleNamespace(
        digits=2, trade_tick_size=0.01, trade_tick_value=1.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01,
        trade_stops_level=0, point=0.01, filling_mode=2,
    )
    monkeypatch.setattr(live_mt5_bot.mt5, "symbol_info", lambda _sym: info)
    monkeypatch.setattr(live_mt5_bot.mt5, "symbol_info_tick", lambda _sym: SimpleNamespace(bid=2403.5, ask=2404.0))
    sent = []
    monkeypatch.setattr(live_mt5_bot.mt5, "order_send", lambda req: sent.append(req))

    count = replay_recent_bars(
        {"XAUUSD": "XAUUSD"},
        balance=100_000.0,
        risk_pct=0.01,
        donchian_period=20,
        sl_atr_mult=2.0,
        max_cost_ratio=0.20,
        replay_bars=10,
        server_offset_hours=7,
    )
    assert count == 1
    assert sent == []
