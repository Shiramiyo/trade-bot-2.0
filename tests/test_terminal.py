"""Unit and HTTP integration tests for the Read-only Trading Terminal."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

import MetaTrader5 as mt5
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_mt5_bot import MAGIC_NUMBER, NY_TZ, write_bot_state
from terminal_data import (
    build_chart_payload,
    compute_live_performance,
    compute_symbol_radar,
    read_bot_heartbeat,
    reconstruct_trades_from_deals,
    summarize_balance_deals,
    tail_bot_log,
)
from terminal_server import create_terminal_server


def _ny_to_broker_epoch(dt_ny: datetime, offset_hours: int = 7) -> int:
    naive = dt_ny.astimezone(NY_TZ).replace(tzinfo=None) + timedelta(hours=offset_hours)
    return int(naive.replace(tzinfo=timezone.utc).timestamp())


def test_reconstruct_trades_and_performance_bot_vs_manual():
    e_in = getattr(mt5, "DEAL_ENTRY_IN", 0)
    e_out = getattr(mt5, "DEAL_ENTRY_OUT", 1)
    t_buy = getattr(mt5, "DEAL_TYPE_BUY", 0)
    t_sell = getattr(mt5, "DEAL_TYPE_SELL", 1)
    t_bal = getattr(mt5, "DEAL_TYPE_BALANCE", 2)

    deals = [
        SimpleNamespace(ticket=1, position_id=0, type=t_bal, entry=e_in, magic=0, time=_ny_to_broker_epoch(datetime(2026, 11, 1, 0, 0, tzinfo=NY_TZ)), time_msc=1, volume=0.0, price=0.0, profit=100000.0, commission=0.0, swap=0.0, fee=0.0, symbol="", comment="Deposit"),
        # Bot BUY trade closed by broker SL/TP exit (exit deal carries magic=0)
        SimpleNamespace(ticket=10, position_id=501, type=t_buy, entry=e_in, magic=MAGIC_NUMBER, time=_ny_to_broker_epoch(datetime(2026, 11, 10, 9, 0, tzinfo=NY_TZ)), time_msc=10, volume=1.0, price=4000.0, profit=0.0, commission=-4.0, swap=0.0, fee=0.0, symbol="XAUUSD", comment="DONCHIAN_1H"),
        SimpleNamespace(ticket=11, position_id=501, type=t_sell, entry=e_out, magic=0, time=_ny_to_broker_epoch(datetime(2026, 11, 10, 14, 0, tzinfo=NY_TZ)), time_msc=11, volume=1.0, price=4025.0, profit=2504.0, commission=0.0, swap=0.0, fee=0.0, symbol="XAUUSD", comment="sl 4025"),
        # Manual trade (magic=0 on entry & exit)
        SimpleNamespace(ticket=20, position_id=502, type=t_sell, entry=e_in, magic=0, time=_ny_to_broker_epoch(datetime(2026, 11, 11, 10, 0, tzinfo=NY_TZ)), time_msc=20, volume=2.0, price=28000.0, profit=0.0, commission=0.0, swap=0.0, fee=0.0, symbol="USTEC", comment="Manual"),
        SimpleNamespace(ticket=21, position_id=502, type=t_buy, entry=e_out, magic=0, time=_ny_to_broker_epoch(datetime(2026, 11, 11, 12, 0, tzinfo=NY_TZ)), time_msc=21, volume=2.0, price=28050.0, profit=-500.0, commission=0.0, swap=0.0, fee=0.0, symbol="USTEC", comment="Manual close"),
        # Still-open trade (no exit deal yet)
        SimpleNamespace(ticket=30, position_id=503, type=t_buy, entry=e_in, magic=MAGIC_NUMBER, time=_ny_to_broker_epoch(datetime(2026, 11, 12, 8, 0, tzinfo=NY_TZ)), time_msc=30, volume=1.0, price=4010.0, profit=0.0, commission=0.0, swap=0.0, fee=0.0, symbol="XAUUSD", comment="DONCHIAN_1H"),
    ]

    bal = summarize_balance_deals(deals)
    assert bal["initial_balance"] == 100000.0

    bot_trades = reconstruct_trades_from_deals(deals, bot_only=True)
    assert len(bot_trades) == 1
    assert bot_trades[0]["symbol"] == "XAUUSD"
    assert bot_trades[0]["net_pnl"] == 2500.0
    assert bot_trades[0]["is_bot"] is True
    assert bot_trades[0]["duration_hours"] == 5.0

    all_trades = reconstruct_trades_from_deals(deals, bot_only=False)
    assert len(all_trades) == 2

    perf = compute_live_performance(all_trades, initial_balance=100000.0, current_balance=102000.0, current_equity=102000.0)
    assert perf["total_trades"] == 2
    assert perf["wins"] == 1
    assert perf["losses"] == 1
    assert perf["win_rate_pct"] == 50.0
    assert perf["net_pnl"] == 2000.0
    assert perf["profit_factor"] == 5.0
    assert len(perf["by_symbol"]) == 2
    assert len(perf["equity_curve"]) == 3  # Start + 2 closed trades


def test_radar_heartbeat_and_log_tail(tmp_path):
    # Synthetic 260 1H bars encoded as MT5 NY+7h epochs
    start_ny = datetime(2026, 10, 1, 0, 0, tzinfo=NY_TZ)
    rates = []
    for i in range(260):
        t_ny = start_ny + timedelta(hours=i)
        epoch = _ny_to_broker_epoch(t_ny, offset_hours=7)
        base = 2400.0 + i * 0.5
        rates.append({"time": epoch, "open": base, "high": base + 2.0, "low": base - 2.0, "close": base + 0.4})

    now_ny = start_ny + timedelta(hours=259, minutes=30)
    info = SimpleNamespace(digits=2, trade_tick_size=0.01, trade_tick_value=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01)
    tick = SimpleNamespace(bid=2529.0, ask=2529.3)

    radar = compute_symbol_radar(
        symbol_map={"XAUUSD": "XAUUSD"},
        rates_by_sym={"XAUUSD": rates},
        ticks_by_sym={"XAUUSD": tick},
        infos_by_sym={"XAUUSD": info},
        balance=100000.0,
        risk_pct=0.01,
        donchian_period=20,
        sl_atr_mult=2.0,
        trail_atr_mult=2.5,
        max_cost_ratio=0.20,
        now_ny=now_ny,
        server_offset_hours=7,
    )
    assert len(radar) == 1
    item = radar[0]
    assert item["status"] == "READY"
    assert item["alignment"] == "BULLISH"
    assert len(item["bars"]) == 60
    assert item["spread_ok"] is True

    # Heartbeat snapshot + log tailing
    state_file = tmp_path / "live_state.json"
    write_bot_state(
        str(state_file),
        {
            "pid": None,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "poll_seconds": 30,
            "strategy": "DONCHIAN_1H",
        },
    )
    hb = read_bot_heartbeat(state_file)
    assert hb["present"] is True
    assert hb["alive"] is True

    log_file = tmp_path / "live_bot.log"
    log_file.write_text(
        "2026-11-15 09:00:00,000 [INFO] Started bot\n"
        "2026-11-15 09:05:00,000 [WARNING] Spread high\n",
        encoding="utf-8",
    )
    lines = tail_bot_log(log_file, limit=10)
    assert len(lines) == 2
    assert lines[0]["level"] == "WARNING"
    assert lines[1]["msg"] == "Started bot"


def test_terminal_http_auth_security_and_endpoints(monkeypatch, tmp_path):
    # Refuse --no-auth on non-loopback host
    with pytest.raises(SystemExit):
        create_terminal_server(host="0.0.0.0", port=0, no_auth=True)

    # Stub MT5 initialization so HTTP test runs cleanly without live MT5 dependency
    monkeypatch.setattr(mt5, "initialize", lambda: False)
    monkeypatch.setattr(mt5, "account_info", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (-1, "offline"))

    passcode = "SecRet99"
    server, effective = create_terminal_server(
        host="127.0.0.1",
        port=0,
        passcode=passcode,
        no_auth=False,
    )
    assert effective == passcode
    port = server.server_port
    base_url = f"http://127.0.0.1:{port}"
    server.data_service.state_path = tmp_path / "missing_state.json"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        # 1. Static index loads with strict security headers
        with opener.open(f"{base_url}/") as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert "<title>TradeTerminal" in html
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert "frame-ancestors 'none'" in (resp.headers.get("Content-Security-Policy") or "")

        # 2. Protected API returns 401 before login
        with pytest.raises(urllib.error.HTTPError) as unauth_err:
            opener.open(f"{base_url}/api/snapshot")
        assert unauth_err.value.code == 401

        # 3. Cross-site Fetch Metadata is rejected
        cross_req = urllib.request.Request(
            f"{base_url}/api/reports",
            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"},
        )
        with pytest.raises(urllib.error.HTTPError) as cs_err:
            opener.open(cross_req)
        assert cs_err.value.code == 403

        # 4. Login with valid passcode sets HttpOnly cookie and unlocks endpoints
        login_req = urllib.request.Request(
            f"{base_url}/api/login",
            data=json.dumps({"passcode": passcode}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(login_req) as login_resp:
            assert login_resp.status == 200
            assert json.loads(login_resp.read())["authenticated"] is True

        with opener.open(f"{base_url}/api/snapshot?scope=all") as snap_resp:
            assert snap_resp.status == 200
            snap = json.loads(snap_resp.read())
            assert snap["mt5_connected"] is False
            assert "performance" in snap

        with opener.open(f"{base_url}/api/reports") as rep_resp:
            assert rep_resp.status == 200
            rep = json.loads(rep_resp.read())
            assert any(r["strategy"] == "DONCHIAN_1H_TREND" for r in rep["strategies"])

        # 5. Logout revokes session
        logout_req = urllib.request.Request(f"{base_url}/api/logout", data=b"", method="POST")
        with opener.open(logout_req) as out_resp:
            assert out_resp.status == 200

        with pytest.raises(urllib.error.HTTPError) as post_logout_err:
            opener.open(f"{base_url}/api/snapshot")
        assert post_logout_err.value.code == 401
    finally:
        server.shutdown()
        server.server_close()


def test_build_chart_payload_and_chart_endpoints(monkeypatch, tmp_path):
    # Synthetic 280 1H bars in a steady uptrend (MT5 NY+7h epoch encoding)
    start_ny = datetime(2026, 10, 1, 0, 0, tzinfo=NY_TZ)
    rates = []
    for i in range(280):
        t_ny = start_ny + timedelta(hours=i)
        epoch = _ny_to_broker_epoch(t_ny, offset_hours=7)
        base = 2400.0 + i * 0.6
        rates.append(
            {
                "time": epoch,
                "open": base,
                "high": base + 2.5,
                "low": base - 2.0,
                "close": base + 0.5,
            }
        )

    now_ny = start_ny + timedelta(hours=279, minutes=30)
    info = SimpleNamespace(
        digits=2,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )
    tick = SimpleNamespace(bid=2567.8, ask=2568.1)

    payload = build_chart_payload(
        mt5_sym="XAUUSD",
        timeframe="1H",
        rates_tf=rates,
        rates_h1=rates,
        tick=tick,
        info=info,
        balance=100000.0,
        risk_pct=0.01,
        bars_limit=120,
        now_ny=now_ny,
        server_offset_hours=7,
    )

    assert payload["symbol"] == "XAUUSD"
    assert payload["timeframe"] == "1H"
    assert payload["digits"] == 2
    assert len(payload["candles"]) == 120

    # Candle timestamps must be strictly increasing UTC epochs
    times = [c["time"] for c in payload["candles"]]
    assert times == sorted(times) and len(set(times)) == len(times)

    ind = payload["indicators"]
    assert len(ind["don_hi"]) > 0
    assert len(ind["don_lo"]) > 0
    assert len(ind["ema50"]) > 0
    assert len(ind["ema200"]) > 0

    fc = payload["forecast"]
    assert fc["recommended"] == "BULLISH"
    assert fc["atr14"] > 0
    assert fc["sl_dist"] > 0
    assert fc["risk_usd"] == 1000.0
    assert fc["est_lots"] > 0
    assert fc["long"]["entry"] > fc["long"]["sl"]
    assert fc["long"]["tp_1r"] < fc["long"]["tp_2r"] < fc["long"]["tp_3r"]
    assert fc["short"]["entry"] < fc["short"]["sl"]
    assert fc["short"]["tp_1r"] > fc["short"]["tp_2r"] > fc["short"]["tp_3r"]

    # HTTP route checks for vendor bundle and /api/chart
    monkeypatch.setattr(mt5, "initialize", lambda: False)
    monkeypatch.setattr(mt5, "account_info", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (-1, "offline"))

    passcode = "ChartTest9"
    server, _ = create_terminal_server(
        host="127.0.0.1",
        port=0,
        passcode=passcode,
        no_auth=False,
    )
    base_url = f"http://127.0.0.1:{server.server_port}"
    server.data_service.state_path = tmp_path / "missing_state.json"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        # Local vendored LightweightCharts bundle must be served from 'self'
        with opener.open(f"{base_url}/vendor/lightweight-charts.js") as js_resp:
            assert js_resp.status == 200
            assert "javascript" in (js_resp.headers.get("Content-Type") or "").lower()
            js_body = js_resp.read(4096).decode("utf-8", errors="ignore")
            assert len(js_body) > 1000

        # /api/chart must reject unauthenticated callers
        with pytest.raises(urllib.error.HTTPError) as unauth_chart:
            opener.open(f"{base_url}/api/chart?symbol=USTEC&tf=1h&bars=360")
        assert unauth_chart.value.code == 401

        # Authenticate and query /api/chart
        login_req = urllib.request.Request(
            f"{base_url}/api/login",
            data=json.dumps({"passcode": passcode}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(login_req) as login_resp:
            assert login_resp.status == 200

        with opener.open(f"{base_url}/api/chart?symbol=USTEC&tf=1h&bars=360") as chart_resp:
            assert chart_resp.status == 200
            chart_json = json.loads(chart_resp.read())
            assert chart_json["symbol"] == "USTEC"
            assert chart_json["timeframe"] == "1h"
            assert chart_json["candles"] == []
            assert chart_json["forecast"] is None
            assert "MT5 not connected" in chart_json["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_pwa_manifest_icons_sw_and_connection_info(monkeypatch, tmp_path):
    monkeypatch.setattr(mt5, "initialize", lambda: False)
    monkeypatch.setattr(mt5, "account_info", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (-1, "offline"))

    passcode = "AppTest77"
    server, _ = create_terminal_server(
        host="127.0.0.1",
        port=0,
        passcode=passcode,
        no_auth=False,
    )
    base_url = f"http://127.0.0.1:{server.server_port}"
    server.data_service.state_path = tmp_path / "missing_state.json"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

        with opener.open(f"{base_url}/manifest.json") as man_resp:
            assert man_resp.status == 200
            man = json.loads(man_resp.read())
            assert man["id"] == "/"
            assert man["display"] == "standalone"
            sizes = {ic["sizes"] for ic in man["icons"]}
            assert {"192x192", "512x512"}.issubset(sizes)

        with opener.open(f"{base_url}/sw.js") as sw_resp:
            assert sw_resp.status == 200
            assert "javascript" in (sw_resp.headers.get("Content-Type") or "").lower()
            assert sw_resp.headers.get("Service-Worker-Allowed") == "/"
            sw_text = sw_resp.read().decode("utf-8")
            assert "/api/" in sw_text

        for png_route in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            with opener.open(f"{base_url}{png_route}") as img_resp:
                assert img_resp.status == 200
                assert img_resp.headers.get("Content-Type") == "image/png"
                assert img_resp.read(8) == b"\x89PNG\r\n\x1a\n"

        with opener.open(f"{base_url}/icon.svg") as svg_resp:
            assert svg_resp.status == 200
            assert "svg" in (svg_resp.headers.get("Content-Type") or "").lower()

        # Connection URLs require authentication
        with pytest.raises(urllib.error.HTTPError) as unauth_conn:
            opener.open(f"{base_url}/api/connection-info")
        assert unauth_conn.value.code == 401

        login_req = urllib.request.Request(
            f"{base_url}/api/login",
            data=json.dumps({"passcode": passcode}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(login_req) as login_resp:
            assert login_resp.status == 200

        with opener.open(f"{base_url}/api/connection-info") as conn_resp:
            assert conn_resp.status == 200
            conn = json.loads(conn_resp.read())
            assert conn["auth_required"] is True
            assert "local" in conn["urls"] and "lan" in conn["urls"] and "tailscale" in conn["urls"]
    finally:
        server.shutdown()
        server.server_close()


def test_market_feed_all_15_symbols_and_tv_endpoints(monkeypatch, tmp_path):
    from market_feed import DEFAULT_WATCHLIST, MarketFeedService, _invert_ohlc_row
    import pandas as pd

    expected_15 = [
        "BTCUSDT.P", "SOLUSDT.P", "GOLD", "EURUSD", "JPYUSD",
        "AUDUSD", "USDCHF", "USDCAD", "GBPUSD", "USOIL",
        "NZDUSD", "DXY", "NAS100", "ETHUSDT.P", "HYPEUSDT.P",
    ]
    assert [s.symbol for s in DEFAULT_WATCHLIST] == expected_15

    # Verify OHLC inversion preserves high >= low invariant
    o, h, l, c = _invert_ohlc_row(158.0, 160.0, 157.0, 159.0)
    assert h >= l and abs(o - 1 / 158.0) < 1e-9 and abs(c - 1 / 159.0) < 1e-9

    # Mock offline HTTP/MT5/Yahoo so unit test is deterministic and fast
    def fake_http(url: str):
        if "ticker/24hr" in url:
            return [
                {"symbol": "BTCUSDT", "lastPrice": "61354.1", "priceChange": "117.2", "priceChangePercent": "0.19"},
                {"symbol": "SOLUSDT", "lastPrice": "72.15", "priceChange": "1.46", "priceChangePercent": "2.07"},
                {"symbol": "ETHUSDT", "lastPrice": "1723.67", "priceChange": "0.34", "priceChangePercent": "0.02"},
                {"symbol": "HYPEUSDT", "lastPrice": "64.122", "priceChange": "1.033", "priceChangePercent": "1.64"},
            ]
        if "klines" in url:
            return [
                [1795300000000 + i * 3600000, "60.0", "65.0", "59.5", "64.1", "1234.5"]
                for i in range(5)
            ]
        return []

    mt5_map = {
        "XAUUSD": (3620.5, 3538.0),
        "EURUSD": (1.09636, 1.09668),
        "USDJPY": (159.08, 158.50),
        "AUDUSD": (0.66382, 0.66198),
        "USDCHF": (0.83855, 0.83959),
        "USDCAD": (1.43393, 1.43434),
        "GBPUSD": (1.31035, 1.30814),
        "NZDUSD": (0.54650, 0.54710),
        "USTEC": (25958.5, 25220.3),
    }
    monkeypatch.setattr(mt5, "initialize", lambda: True)
    monkeypatch.setattr(mt5, "account_info", lambda: SimpleNamespace(login=1))
    monkeypatch.setattr(mt5, "symbol_select", lambda sym, flag: sym in mt5_map)
    monkeypatch.setattr(mt5, "symbol_info", lambda sym: SimpleNamespace(digits=3 if sym == "USDJPY" else 5) if sym in mt5_map else None)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda sym: SimpleNamespace(bid=mt5_map[sym][0], ask=mt5_map[sym][0]) if sym in mt5_map else None)
    monkeypatch.setattr(
        mt5,
        "copy_rates_from_pos",
        lambda sym, tf, pos, n: [
            {"time": 1795200000 + i * 3600, "open": mt5_map[sym][1], "high": mt5_map[sym][0] * 1.001, "low": mt5_map[sym][1] * 0.999, "close": mt5_map[sym][1] if i < 2 else mt5_map[sym][0], "tick_volume": 100}
            for i in range(3)
        ] if sym in mt5_map else None,
    )

    import market_feed
    idx = pd.date_range("2026-11-20", periods=3, freq="1D", tz="UTC")
    yf_df = pd.DataFrame(
        {("Close", "CL=F"): [83.0, 83.6, 82.31], ("Close", "DX-Y.NYB"): [103.5, 103.84, 103.891]},
        index=idx,
    )
    yf_df.columns = pd.MultiIndex.from_tuples(yf_df.columns)
    monkeypatch.setattr(market_feed.yf, "download", lambda *a, **kw: yf_df)

    feed = MarketFeedService(http_getter=fake_http)
    wl = feed.get_watchlist()
    by_sym = {r["symbol"]: r for r in wl["symbols"]}
    assert list(by_sym.keys()) == expected_15
    assert by_sym["BTCUSDT.P"]["price"] == 61354.1
    assert by_sym["HYPEUSDT.P"]["price"] == 64.122
    assert by_sym["USOIL"]["price"] == 82.31
    assert by_sym["DXY"]["price"] == 103.891
    assert abs(by_sym["JPYUSD"]["price"] - (1 / 159.08)) < 1e-6

    # HTTP route checks for /tv_chart.js, /api/tv/watchlist, and /api/tv/chart
    passcode = "TVPass88"
    server, _ = create_terminal_server(host="127.0.0.1", port=0, passcode=passcode, no_auth=False)
    server.market_feed = feed
    server.data_service.state_path = tmp_path / "missing_state.json"
    base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        with opener.open(f"{base_url}/tv_chart.js") as js_resp:
            assert js_resp.status == 200
            assert "TVStudio" in js_resp.read().decode("utf-8", errors="ignore")

        with pytest.raises(urllib.error.HTTPError) as unauth_wl:
            opener.open(f"{base_url}/api/tv/watchlist")
        assert unauth_wl.value.code == 401

        login_req = urllib.request.Request(
            f"{base_url}/api/login",
            data=json.dumps({"passcode": passcode}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(login_req) as login_resp:
            assert login_resp.status == 200

        with opener.open(f"{base_url}/api/tv/watchlist") as wl_resp:
            assert wl_resp.status == 200
            wl_json = json.loads(wl_resp.read())
            assert len(wl_json["symbols"]) == 15

        with opener.open(f"{base_url}/api/tv/chart?symbol=HYPEUSDT.P&tf=15m&bars=120") as ch_resp:
            assert ch_resp.status == 200
            ch_json = json.loads(ch_resp.read())
            assert ch_json["symbol"] == "HYPEUSDT.P"
            assert len(ch_json["candles"]) == 5
    finally:
        server.shutdown()
        server.server_close()


def test_market_feed_hyperliquid_fallback_and_mt5_optional(monkeypatch) -> None:
    import market_feed
    import pandas as pd
    from market_feed import DEFAULT_WATCHLIST, MarketFeedService

    monkeypatch.setattr(market_feed, "mt5", None)

    def blocked_binance(url: str):
        raise urllib.error.HTTPError(url, 451, "Unavailable For Legal Reasons", {}, None)

    def fake_hl_post(url: str, payload):
        assert "hyperliquid" in url
        if payload.get("type") == "metaAndAssetCtxs":
            return [
                {"universe": [{"name": "BTC"}, {"name": "SOL"}, {"name": "ETH"}, {"name": "HYPE"}, {"name": "DOGE"}]},
                [
                    {"midPx": "91250.5", "prevDayPx": "90000.0", "dayNtlVlm": "5000000"},
                    {"midPx": "182.45", "prevDayPx": "180.0", "dayNtlVlm": "1000000"},
                    {"midPx": "3120.75", "prevDayPx": "3050.0", "dayNtlVlm": "2000000"},
                    {"midPx": "28.45", "prevDayPx": "26.0", "dayNtlVlm": "800000"},
                    {"midPx": "0.3912", "prevDayPx": "0.38", "dayNtlVlm": "400000"},
                ],
            ]
        if payload.get("type") == "candleSnapshot":
            return [
                {"t": 1795300000000 + i * 3600000, "o": "27.5", "h": "29.0", "l": "27.1", "c": "28.45", "v": "1234.5"}
                for i in range(5)
            ]
        return []

    seen_yf_tickers = []

    def fake_yf_download(tickers, **kwargs):
        seen_yf_tickers.extend(tickers)
        idx = pd.date_range("2026-11-20", periods=3, freq="1D", tz="UTC")
        cols = {}
        for i, tk in enumerate(tickers):
            base = 100.0 + i
            cols[("Close", tk)] = [base - 1.0, base - 0.5, base]
        df = pd.DataFrame(cols, index=idx)
        df.columns = pd.MultiIndex.from_tuples(df.columns)
        return df

    monkeypatch.setattr(market_feed.yf, "download", fake_yf_download)

    feed = MarketFeedService(http_getter=blocked_binance, http_poster=fake_hl_post)
    wl = feed.get_watchlist(extra_symbols=["DOGEUSDT.P"], force=True)
    by_sym = {r["symbol"]: r for r in wl["symbols"]}

    for spec in DEFAULT_WATCHLIST:
        assert by_sym[spec.symbol]["price"] is not None

    # All 4 crypto perps satisfied by Hyperliquid without hitting Yahoo
    assert "BTC-USD" not in seen_yf_tickers
    assert by_sym["BTCUSDT.P"]["source"] == "Hyperliquid BTC-USD"
    assert by_sym["BTCUSDT.P"]["price"] == 91250.5
    assert by_sym["HYPEUSDT.P"]["source"] == "Hyperliquid HYPE-USD"
    assert by_sym["HYPEUSDT.P"]["price"] == 28.45
    assert by_sym["DOGEUSDT.P"]["source"] == "Hyperliquid DOGE-USD"
    assert by_sym["DOGEUSDT.P"]["price"] == 0.3912
    assert by_sym["DOGEUSDT.P"]["digits"] == 5

    chart = feed.get_chart("HYPEUSDT.P", timeframe="1h", bars=100)
    assert chart["source"] == "Hyperliquid Perp (HYPE-USD)"
    assert len(chart["candles"]) == 5
    assert chart["price"] == 28.45


def test_public_showcase_mode_sanitizes_private_mt5(monkeypatch, tmp_path: Path) -> None:
    import terminal_data

    monkeypatch.setattr(terminal_data, "mt5", None)

    # Regular --no-auth remains blocked on 0.0.0.0
    with pytest.raises(SystemExit):
        create_terminal_server(host="0.0.0.0", port=0, no_auth=True)

    # Public showcase allows 0.0.0.0 creation safely
    s0, code0 = create_terminal_server(host="0.0.0.0", port=0, public_showcase=True)
    assert code0 == ""
    s0.server_close()

    state_file = tmp_path / "live_state.json"
    state_file.write_text(json.dumps({"account_login": 999888777, "account_balance": 123456.78}), encoding="utf-8")
    log_file = tmp_path / "live_bot.log"
    log_file.write_text("2026-11-25 10:00:00 [INFO] SECRET_ACCOUNT_LOG 999888777\n", encoding="utf-8")
    reports_file = tmp_path / "arena_4y_results.csv"
    reports_file.write_text("strategy,net_pnl\nDONCHIAN_1H,12500\n", encoding="utf-8")

    server, _ = create_terminal_server(host="127.0.0.1", port=0, public_showcase=True)
    server.data_service.state_path = state_file
    server.data_service.log_path = log_file
    server.data_service.reports_path = reports_file

    base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"{base_url}/api/health") as h_resp:
            h_json = json.loads(h_resp.read())
            assert h_json["public_showcase"] is True
            assert h_json["auth_required"] is False

        with urllib.request.urlopen(f"{base_url}/api/auth") as a_resp:
            a_json = json.loads(a_resp.read())
            assert a_json["authenticated"] is True
            assert a_json["public_showcase"] is True

        with urllib.request.urlopen(f"{base_url}/api/connection-info") as c_resp:
            c_json = json.loads(c_resp.read())
            assert c_json["urls"] == {"local": [], "lan": [], "tailscale": []}

        with urllib.request.urlopen(f"{base_url}/api/snapshot") as s_resp:
            s_raw = s_resp.read().decode("utf-8")
            assert "999888777" not in s_raw
            s_json = json.loads(s_raw)
            assert s_json["public_showcase"] is True
            assert s_json["account"]["login"] is None
            assert s_json["account"]["mode"] == "SHOWCASE"
            assert s_json["positions"] == []

        with urllib.request.urlopen(f"{base_url}/api/logs") as l_resp:
            l_raw = l_resp.read().decode("utf-8")
            assert "SECRET_ACCOUNT_LOG" not in l_raw

        with urllib.request.urlopen(f"{base_url}/api/reports") as r_resp:
            r_json = json.loads(r_resp.read())
            assert len(r_json["strategies"]) == 1
            assert r_json["strategies"][0]["strategy"] == "DONCHIAN_1H"

        with urllib.request.urlopen(f"{base_url}/api/chart?symbol=USTEC") as lc_resp:
            lc_json = json.loads(lc_resp.read())
            assert "Public Showcase" in lc_json["error"]

        login_req = urllib.request.Request(
            f"{base_url}/api/login",
            data=json.dumps({"passcode": "ignored"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(login_req) as login_resp:
            assert json.loads(login_resp.read())["public_showcase"] is True
    finally:
        server.shutdown()
        server.server_close()


