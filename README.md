# ICT/SMC Multi-Asset Trading Bot & Backtester

Modular Python backtester for the 10-symbol ICT/SMC strategy:
- **Forex Majors:** `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF`, `NZDUSD`
- **Crypto:** `BTCUSD`
- **Metal:** `XAUUSD`
- **Commodities / Indices:** `CL`, `NQ`

## Quick Start

```powershell
# Run unit & terminal tests
.\.venv\Scripts\python.exe -m pytest -v

# Run a 59-day sample backtest using Yahoo Finance 5m data
.\.venv\Scripts\python.exe run_backtest.py --source yfinance --period 59d

# Run with local 5m CSVs and a high-impact news calendar
.\.venv\Scripts\python.exe run_backtest.py --source csv --csv-dir .\data --news .\sample_news.csv
```

Outputs:
- `trades.csv`: Full trade-by-trade log (entry, SL, TP, exit reason, RR, PnL).
- `backtest_report.html`: Interactive Plotly equity curve and per-symbol PnL chart.

## Best Overall Bot (`NQ + XAUUSD` 1H Donchian Trend)

```powershell
# Run 4-year comparison arena across 16 strategies on calibrated MT5 data
.\.venv\Scripts\python.exe strategy_arena.py

# Dry-run replay on weekend/closed markets using last N closed MT5 1H bars
.\.venv\Scripts\python.exe live_mt5_bot.py --dry-run --once --replay-bars 168

# Dry-run live MT5 scan (no orders sent)
.\.venv\Scripts\python.exe live_mt5_bot.py --dry-run --once

# Run on MT5 Demo account (Demo-only by default; Algo Trading must be enabled in MT5)
.\.venv\Scripts\python.exe live_mt5_bot.py
```

## Web Trading Terminal & Built-in TradingView Chart Studio

Run alongside `live_mt5_bot.py` (or standalone) to monitor live MT5 balance/equity, open positions, closed deal history, `1H` Donchian radar, 4-year arena reports, `live_bot.log`, and a built-in **TradingView-style charting terminal**:

- **15-Symbol Multi-Source Watchlist** (`market_feed.py`): Live quotes & OHLCV across Binance Futures (`BTCUSDT.P`, `SOLUSDT.P`, `ETHUSDT.P`, `HYPEUSDT.P`), MetaTrader 5 (`GOLD`, `NAS100`, `EURUSD`, `JPYUSD`, `AUDUSD`, `USDCHF`, `USDCAD`, `GBPUSD`, `NZDUSD`), and Yahoo Finance (`USOIL`, `DXY`), plus custom ticker lookup/add and color flagging.
- **Charting & Analysis** (`web/tv_chart.js`): Timeframes (`1m`–`1W`), 6 chart styles (including Heikin-Ashi), unlimited indicators & oscillator sub-panes (`EMA 20/50/200`, `SMA 20`, `Donchian 20`, `Bollinger Bands`, `VWAP`, `PDH/PDL`, `ICT FVG`, `Killzone Sessions`, `Volume`, `RSI`, `MACD`, `ATR`), interactive drawing tools persisted per symbol (Trendline, Ray, H/V Lines, Rectangle, Fib Retracement, Long/Short R:R boxes, Ruler, Text, Magnet Snap), Bar Replay, 1/2/4-pane layouts, Price Alerts, and PNG export.

```powershell
# Launch authenticated web dashboard on port 8080 (prints passcode + PC/Wi-Fi/Tailscale URLs)
.\.venv\Scripts\python.exe terminal_server.py

# Optional: custom passcode or PC-only loopback without passcode
.\.venv\Scripts\python.exe terminal_server.py --passcode MyStrongPin99
.\.venv\Scripts\python.exe terminal_server.py --host 127.0.0.1 --no-auth
```

To check safely from your phone anywhere over 4G/5G without exposing your PC to the public internet:
1. Install **Tailscale** (free) on both your Windows PC and your phone, signed into the same Tailscale account.
2. Start `.\.venv\Scripts\python.exe terminal_server.py` (allow Python in Windows Firewall on Private networks if prompted).
3. Open the printed **Tailscale** URL (`http://100.x.y.z:8080`) or **Home Wi-Fi** URL (`http://<PC-LAN-IP>:8080`) on your phone browser and unlock with your passcode.

## Deploy Online (Public Showcase)

Host the built-in TradingView Chart Studio (`15` multi-source watchlist symbols via Binance -> Hyperliquid -> Yahoo Finance) and 4-Year Strategy Arena benchmark publicly on Linux PaaS (Render, Railway, Fly.io, or Docker) without leaking MT5 credentials, `.terminal_passcode`, `live_state.json`, or `live_bot.log`:

```bash
# Linux / Cloud install (skips Windows-only MetaTrader5 wheel)
pip install -r requirements-cloud.txt

# Launch sanitized public showcase (auto-uses $PORT when set, defaults Landing Tab -> Chart)
python terminal_server.py --host 0.0.0.0 --public-showcase
```

Included deployment templates:
- [`render.yaml`](render.yaml) — One-click Render Web Service blueprint (`free` plan, `PUBLIC_SHOWCASE=1`, `/api/health` healthcheck).
- [`Dockerfile`](Dockerfile) — Portable container image (`python:3.12-slim`) for Railway, Fly.io, or self-hosted Docker.
- [`requirements-cloud.txt`](requirements-cloud.txt) — Pure-Python cloud dependencies without `MetaTrader5`.
