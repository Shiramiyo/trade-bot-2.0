"use strict";

(() => {
  const STORAGE_KEY_SETTINGS = "tv_studio_settings_v1";
  const STORAGE_KEY_DRAWINGS = "tv_studio_drawings_v1";
  const STORAGE_KEY_FLAGS = "tv_studio_flags_v1";
  const STORAGE_KEY_CUSTOM_SYMS = "tv_studio_custom_syms_v1";
  const STORAGE_KEY_ALERTS = "tv_studio_alerts_v1";

  const SYMBOL_ALIAS_TO_TV = {
    USTEC: "NAS100",
    NQ: "NAS100",
    US100: "NAS100",
    XAUUSD: "GOLD",
    CL: "USOIL",
    BTCUSD: "BTCUSDT.P",
    ETHUSD: "ETHUSDT.P",
    SOLUSD: "SOLUSDT.P",
  };

  const DEFAULT_INDICATORS = {
    ema20: false,
    ema50: true,
    ema200: true,
    sma20: false,
    donchian: true,
    bb: false,
    vwap: false,
    pdhl: false,
    fvg: false,
    sessions: false,
    volume: true,
    rsi: false,
    macd: false,
    atr: false,
  };

  const FIB_LEVELS = [
    { r: 0, color: "#94a3b8" },
    { r: 0.236, color: "#f87171" },
    { r: 0.382, color: "#f59e0b" },
    { r: 0.5, color: "#22c55e" },
    { r: 0.618, color: "#38bdf8" },
    { r: 0.786, color: "#a855f7" },
    { r: 1, color: "#94a3b8" },
  ];

  const DEFAULT_PANE_TEMPLATES = [
    { symbol: "NAS100", tf: "1h" },
    { symbol: "GOLD", tf: "1h" },
    { symbol: "BTCUSDT.P", tf: "15m" },
    { symbol: "EURUSD", tf: "1h" },
  ];

  const studio = {
    initialized: false,
    visible: false,
    layout: 1,
    activePaneIdx: 0,
    syncCrosshair: true,
    syncSymbol: false,
    chartStyle: "candles",
    timezone: "Asia/Phnom_Penh",
    scalePct: false,
    scaleLog: false,
    scaleInv: false,
    scaleAuto: true,
    showBotBoxes: true,
    indicators: { ...DEFAULT_INDICATORS },
    tool: "cursor",
    magnet: false,
    locked: false,
    watchlistCollapsed: false,
    watchlistFilter: "ALL",
    watchlistSearch: "",
    watchlistRows: [],
    flags: {},
    customSymbols: [],
    alerts: [],
    drawingsBySymbol: {},
    undoStack: [],
    selectedDrawing: null, // { symbol, id }
    replay: {
      active: false,
      picking: false,
      cutoffIdx: null,
      playing: false,
      speedMs: 800,
      timerId: null,
    },
    panes: [], // runtime pane objects
    wlTimerId: null,
    snapshotRef: null,
    inspectedTrade: null,
    targetR: 3,
    apiJsonFn: null,
    onStatusAlert: null,
  };

  const $ = (id) => document.getElementById(id);

  function loadJsonStorage(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (_) {
      return fallback;
    }
  }

  function saveJsonStorage(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (_) {
      // Ignore quota errors
    }
  }

  function loadPersistedState() {
    const saved = loadJsonStorage(STORAGE_KEY_SETTINGS, {});
    if (saved.layout && [1, 2, 4].includes(saved.layout)) studio.layout = saved.layout;
    if (saved.chartStyle) studio.chartStyle = saved.chartStyle;
    if (saved.timezone) studio.timezone = saved.timezone;
    if (typeof saved.showBotBoxes === "boolean") studio.showBotBoxes = saved.showBotBoxes;
    if (typeof saved.syncCrosshair === "boolean") studio.syncCrosshair = saved.syncCrosshair;
    if (typeof saved.syncSymbol === "boolean") studio.syncSymbol = saved.syncSymbol;
    if (typeof saved.magnet === "boolean") studio.magnet = saved.magnet;
    if (typeof saved.locked === "boolean") studio.locked = saved.locked;
    if (saved.indicators) studio.indicators = { ...DEFAULT_INDICATORS, ...saved.indicators };

    studio.flags = loadJsonStorage(STORAGE_KEY_FLAGS, {});
    studio.customSymbols = loadJsonStorage(STORAGE_KEY_CUSTOM_SYMS, []);
    studio.alerts = loadJsonStorage(STORAGE_KEY_ALERTS, []);
    studio.drawingsBySymbol = loadJsonStorage(STORAGE_KEY_DRAWINGS, {});

    const paneConfigs = Array.isArray(saved.panes) && saved.panes.length === 4 ? saved.panes : DEFAULT_PANE_TEMPLATES;
    studio.panes = paneConfigs.map((cfg, idx) => ({
      idx,
      symbol: cfg.symbol || DEFAULT_PANE_TEMPLATES[idx].symbol,
      tf: cfg.tf || DEFAULT_PANE_TEMPLATES[idx].tf,
      data: null,
      rawCandles: [],
      displayCandles: [],
      barStepSec: 3600,
      chart: null,
      mainSeries: null,
      overlaySeries: {},
      subPanes: {}, // key -> { pane, seriesMap }
      markersApi: null,
      primitive: null,
      ro: null,
      el: null,
      canvasHost: null,
      legendMainEl: null,
      legendIndsEl: null,
      interaction: null, // drag/draw runtime state
    }));
  }

  function persistSettings() {
    saveJsonStorage(STORAGE_KEY_SETTINGS, {
      layout: studio.layout,
      chartStyle: studio.chartStyle,
      timezone: studio.timezone,
      showBotBoxes: studio.showBotBoxes,
      syncCrosshair: studio.syncCrosshair,
      syncSymbol: studio.syncSymbol,
      magnet: studio.magnet,
      locked: studio.locked,
      indicators: studio.indicators,
      panes: studio.panes.map((p) => ({ symbol: p.symbol, tf: p.tf })),
    });
  }

  function announceStatus(msg) {
    const el = $("tv-action-status");
    if (el) el.textContent = msg;
    const hint = $("tv-draw-hint");
    if (hint && msg) hint.textContent = msg;
  }

  function formatNum(val, digits = 2) {
    const n = Number(val);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function formatSigned(val, digits = 2, suffix = "") {
    const n = Number(val);
    if (!Number.isFinite(n)) return "—";
    const sign = n > 0 ? "+" : n < 0 ? "-" : "";
    const abs = Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    return `${sign}${abs}${suffix}`;
  }

  function renderPricePipette(container, val, digits = 2) {
    container.textContent = "";
    if (!Number.isFinite(Number(val))) {
      container.textContent = "—";
      return;
    }
    const formatted = formatNum(val, digits);
    if (digits >= 3 && formatted.length > 2) {
      const head = formatted.slice(0, -1);
      const tail = formatted.slice(-1);
      container.append(document.createTextNode(head));
      const sup = document.createElement("span");
      sup.className = "tv-pip-tail";
      sup.textContent = tail;
      container.append(sup);
    } else {
      container.textContent = formatted;
    }
  }

  function getAxisFormatter() {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: studio.timezone || "Asia/Phnom_Penh",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function getSessionNyParts(epochSec) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date(epochSec * 1000));
    const map = {};
    for (const p of parts) map[p.type] = p.value;
    return {
      dateKey: `${map.year}-${map.month}-${map.day}`,
      hour: Number(map.hour) % 24,
      minute: Number(map.minute),
    };
  }

  // ===== Technical Indicators & ICT Overlays =====
  function computeSMA(candles, period) {
    const out = [];
    let sum = 0;
    for (let i = 0; i < candles.length; i += 1) {
      sum += candles[i].close;
      if (i >= period) sum -= candles[i - period].close;
      if (i >= period - 1) out.push({ time: candles[i].time, value: sum / period });
    }
    return out;
  }

  function computeEMA(candles, period) {
    if (!candles.length) return [];
    const k = 2 / (period + 1);
    let ema = candles[0].close;
    const out = [{ time: candles[0].time, value: ema }];
    for (let i = 1; i < candles.length; i += 1) {
      ema = candles[i].close * k + ema * (1 - k);
      out.push({ time: candles[i].time, value: ema });
    }
    return out;
  }

  function computeDonchian(candles, period = 20) {
    const hi = [];
    const lo = [];
    for (let i = period; i < candles.length; i += 1) {
      let maxH = -Infinity;
      let minL = Infinity;
      for (let j = i - period; j < i; j += 1) {
        if (candles[j].high > maxH) maxH = candles[j].high;
        if (candles[j].low < minL) minL = candles[j].low;
      }
      hi.push({ time: candles[i].time, value: maxH });
      lo.push({ time: candles[i].time, value: minL });
    }
    return { hi, lo };
  }

  function computeBollinger(candles, period = 20, mult = 2) {
    const upper = [];
    const basis = [];
    const lower = [];
    for (let i = period - 1; i < candles.length; i += 1) {
      let sum = 0;
      for (let j = i - period + 1; j <= i; j += 1) sum += candles[j].close;
      const mean = sum / period;
      let sq = 0;
      for (let j = i - period + 1; j <= i; j += 1) {
        const diff = candles[j].close - mean;
        sq += diff * diff;
      }
      const std = Math.sqrt(sq / period);
      const t = candles[i].time;
      basis.push({ time: t, value: mean });
      upper.push({ time: t, value: mean + mult * std });
      lower.push({ time: t, value: mean - mult * std });
    }
    return { upper, basis, lower };
  }

  function computeVWAP(candles, windowSize = 24) {
    const out = [];
    for (let i = 0; i < candles.length; i += 1) {
      const start = Math.max(0, i - windowSize + 1);
      let pv = 0;
      let vSum = 0;
      let tpSum = 0;
      for (let j = start; j <= i; j += 1) {
        const c = candles[j];
        const tp = (c.high + c.low + c.close) / 3;
        const vol = Math.max(0, Number(c.volume || 0));
        pv += tp * vol;
        vSum += vol;
        tpSum += tp;
      }
      const val = vSum > 0 ? pv / vSum : tpSum / (i - start + 1);
      out.push({ time: candles[i].time, value: val });
    }
    return out;
  }

  function computeRSI(candles, period = 14) {
    if (candles.length <= period) return [];
    const out = [];
    let avgGain = 0;
    let avgLoss = 0;
    for (let i = 1; i <= period; i += 1) {
      const diff = candles[i].close - candles[i - 1].close;
      if (diff >= 0) avgGain += diff;
      else avgLoss += Math.abs(diff);
    }
    avgGain /= period;
    avgLoss /= period;
    const firstRs = avgLoss === 0 ? 100 : avgGain / avgLoss;
    out.push({ time: candles[period].time, value: avgLoss === 0 ? 100 : 100 - 100 / (1 + firstRs) });

    for (let i = period + 1; i < candles.length; i += 1) {
      const diff = candles[i].close - candles[i - 1].close;
      const gain = diff > 0 ? diff : 0;
      const loss = diff < 0 ? Math.abs(diff) : 0;
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
      const rsi = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
      out.push({ time: candles[i].time, value: rsi });
    }
    return out;
  }

  function computeMACD(candles, fast = 12, slow = 26, sig = 9) {
    if (candles.length < slow + sig) return { macd: [], signal: [], hist: [] };
    const emaFast = computeEMA(candles, fast);
    const emaSlow = computeEMA(candles, slow);
    const macdRaw = candles.map((c, i) => ({
      time: c.time,
      value: emaFast[i].value - emaSlow[i].value,
    }));

    const kSig = 2 / (sig + 1);
    let sigVal = macdRaw[0].value;
    const macd = [];
    const signal = [];
    const hist = [];
    for (let i = 0; i < macdRaw.length; i += 1) {
      sigVal = macdRaw[i].value * kSig + sigVal * (1 - kSig);
      if (i >= slow - 1) {
        const diff = macdRaw[i].value - sigVal;
        macd.push(macdRaw[i]);
        signal.push({ time: macdRaw[i].time, value: sigVal });
        hist.push({
          time: macdRaw[i].time,
          value: diff,
          color: diff >= 0 ? "rgba(34, 197, 94, 0.72)" : "rgba(248, 113, 113, 0.72)",
        });
      }
    }
    return { macd, signal, hist };
  }

  function computeATR(candles, period = 14) {
    if (candles.length < 2) return [];
    const tr = [candles[0].high - candles[0].low];
    for (let i = 1; i < candles.length; i += 1) {
      const c = candles[i];
      const prevClose = candles[i - 1].close;
      tr.push(Math.max(c.high - c.low, Math.abs(c.high - prevClose), Math.abs(c.low - prevClose)));
    }
    const out = [];
    let atr = tr.slice(0, Math.min(period, tr.length)).reduce((a, b) => a + b, 0) / Math.min(period, tr.length);
    for (let i = period - 1; i < candles.length; i += 1) {
      if (i >= period) atr = (atr * (period - 1) + tr[i]) / period;
      out.push({ time: candles[i].time, value: atr });
    }
    return out;
  }

  function computeHeikinAshi(candles) {
    if (!candles.length) return [];
    const out = [];
    let prevOpen = (candles[0].open + candles[0].close) / 2;
    let prevClose = (candles[0].open + candles[0].high + candles[0].low + candles[0].close) / 4;
    for (let i = 0; i < candles.length; i += 1) {
      const c = candles[i];
      const haClose = (c.open + c.high + c.low + c.close) / 4;
      const haOpen = i === 0 ? prevOpen : (prevOpen + prevClose) / 2;
      const haHigh = Math.max(c.high, haOpen, haClose);
      const haLow = Math.min(c.low, haOpen, haClose);
      out.push({ ...c, open: haOpen, high: haHigh, low: haLow, close: haClose });
      prevOpen = haOpen;
      prevClose = haClose;
    }
    return out;
  }

  function computePDHL(candles) {
    if (candles.length < 2) return null;
    const byDay = new Map();
    for (const c of candles) {
      const dayKey = new Date(c.time * 1000).toISOString().slice(0, 10);
      const entry = byDay.get(dayKey) || { high: -Infinity, low: Infinity };
      entry.high = Math.max(entry.high, c.high);
      entry.low = Math.min(entry.low, c.low);
      byDay.set(dayKey, entry);
    }
    const keys = [...byDay.keys()].sort();
    if (keys.length < 2) return null;
    const prev = byDay.get(keys[keys.length - 2]);
    return { pdh: prev.high, pdl: prev.low, day: keys[keys.length - 2] };
  }

  function computeFVGs(candles, maxCount = 12) {
    const zones = [];
    for (let i = 1; i < candles.length - 1; i += 1) {
      const a = candles[i - 1];
      const c = candles[i + 1];
      let zone = null;
      if (a.high < c.low) {
        zone = { dir: "BULL", low: a.high, high: c.low, startIdx: i - 1 };
      } else if (a.low > c.high) {
        zone = { dir: "BEAR", low: c.high, high: a.low, startIdx: i - 1 };
      }
      if (!zone) continue;
      // Check if subsequent candles fully filled the gap
      let filled = false;
      for (let k = i + 2; k < candles.length; k += 1) {
        if (zone.dir === "BULL" && candles[k].low <= zone.low) {
          filled = true;
          break;
        }
        if (zone.dir === "BEAR" && candles[k].high >= zone.high) {
          filled = true;
          break;
        }
      }
      if (!filled) zones.push(zone);
    }
    return zones.slice(-maxCount);
  }

  function computeSessions(candles, tf) {
    if (!["1m", "5m", "15m", "30m", "1h"].includes(tf)) return [];
    const bands = [];
    let current = null;
    for (let i = 0; i < candles.length; i += 1) {
      const { hour } = getSessionNyParts(candles[i].time);
      let sess = null;
      if (hour >= 19 || hour < 2) sess = "ASIA";
      else if (hour >= 2 && hour < 6) sess = "LONDON";
      else if (hour >= 8 && hour < 12) sess = "NEW YORK";

      if (sess) {
        if (current && current.sess === sess && i - current.endIdx <= 2) {
          current.endIdx = i;
        } else {
          if (current) bands.push(current);
          current = { sess, startIdx: i, endIdx: i };
        }
      } else if (current) {
        bands.push(current);
        current = null;
      }
    }
    if (current) bands.push(current);
    return bands.slice(-18);
  }

  // ===== Coordinate / Bar Time Mapping Helpers =====
  function findBarIndex(candles, epochSec) {
    if (!candles.length) return 0;
    if (epochSec <= candles[0].time) return 0;
    let lo = 0;
    let hi = candles.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (candles[mid].time === epochSec) return mid;
      if (candles[mid].time < epochSec) lo = mid + 1;
      else hi = mid - 1;
    }
    return Math.max(0, hi);
  }

  function inferBarStepSec(candles) {
    if (candles.length < 2) return 3600;
    const diffs = [];
    for (let i = Math.max(1, candles.length - 15); i < candles.length; i += 1) {
      const d = candles[i].time - candles[i - 1].time;
      if (d > 0) diffs.push(d);
    }
    diffs.sort((a, b) => a - b);
    return diffs.length ? diffs[Math.floor(diffs.length / 2)] : 3600;
  }

  function logicalToTime(pane, logical) {
    const candles = pane.displayCandles;
    if (!candles.length) return Math.floor(Date.now() / 1000);
    const step = pane.barStepSec || 3600;
    const idx = Math.round(logical);
    if (idx < 0) return candles[0].time + idx * step;
    if (idx >= candles.length) return candles[candles.length - 1].time + (idx - (candles.length - 1)) * step;
    return candles[idx].time;
  }

  function timeToLogical(pane, epochSec) {
    const candles = pane.displayCandles;
    if (!candles.length) return 0;
    const step = pane.barStepSec || 3600;
    if (epochSec < candles[0].time) {
      return (epochSec - candles[0].time) / step;
    }
    const lastIdx = candles.length - 1;
    if (epochSec > candles[lastIdx].time) {
      return lastIdx + (epochSec - candles[lastIdx].time) / step;
    }
    return findBarIndex(candles, epochSec);
  }

  function getPointPixel(pane, pt) {
    if (!pane.chart || !pane.mainSeries) return null;
    const logical = timeToLogical(pane, pt.time);
    const x = pane.chart.timeScale().logicalToCoordinate(logical);
    const y = pane.mainSeries.priceToCoordinate(pt.price);
    if (x === null || y === null) return null;
    return { x, y, logical };
  }

  function pixelToDataPoint(pane, cssX, cssY, useMagnet = false) {
    if (!pane.chart || !pane.mainSeries) return null;
    const ts = pane.chart.timeScale();
    const logical = ts.coordinateToLogical(cssX);
    let price = pane.mainSeries.coordinateToPrice(cssY);
    if (logical === null || price === null) return null;

    const candles = pane.displayCandles;
    const snappedLogical = Math.round(logical);
    let time = logicalToTime(pane, snappedLogical);

    if (useMagnet && candles.length) {
      const idx = Math.max(0, Math.min(candles.length - 1, snappedLogical));
      const bar = candles[idx];
      const candidates = [bar.open, bar.high, bar.low, bar.close];
      let bestPrice = price;
      let bestDistPx = 18;
      for (const cp of candidates) {
        const cy = pane.mainSeries.priceToCoordinate(cp);
        if (cy !== null && Math.abs(cy - cssY) < bestDistPx) {
          bestDistPx = Math.abs(cy - cssY);
          bestPrice = cp;
        }
      }
      price = bestPrice;
      time = bar.time;
    }
    return { time, price, logical: snappedLogical };
  }

  function distToSegment(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const lenSq = dx * dx + dy * dy;
    if (lenSq === 0) return Math.hypot(px - x1, py - y1);
    const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lenSq));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  // ===== Combined Canvas Primitive (Drawings + Bot Boxes + ICT FVG/Sessions/PDHL) =====
  class TVOverlayPrimitive {
    constructor(pane) {
      this._pane = pane;
      this._chart = null;
      this._series = null;
      this._requestUpdate = null;
      this._botBoxes = [];
      this._fvgs = [];
      this._sessions = [];
      this._pdhl = null;
      this._draft = null;
      this._magnetSnap = null;
      this._paneView = {
        zOrder: () => "normal",
        renderer: () => ({
          draw: (target) => this._draw(target),
        }),
      };
    }

    attached({ chart, series, requestUpdate }) {
      this._chart = chart;
      this._series = series;
      this._requestUpdate = requestUpdate;
    }

    detached() {
      this._chart = null;
      this._series = null;
      this._requestUpdate = null;
    }

    requestRedraw() {
      if (this._requestUpdate) this._requestUpdate();
    }

    setOverlays({ botBoxes, fvgs, sessions, pdhl }) {
      this._botBoxes = botBoxes || [];
      this._fvgs = fvgs || [];
      this._sessions = sessions || [];
      this._pdhl = pdhl || null;
      this.requestRedraw();
    }

    setDraft(draft) {
      this._draft = draft;
      this.requestRedraw();
    }

    setMagnetSnap(snap) {
      this._magnetSnap = snap;
      this.requestRedraw();
    }

    paneViews() {
      return [this._paneView];
    }

    _draw(target) {
      const pane = this._pane;
      if (!this._chart || !this._series) return;
      const ts = this._chart.timeScale();
      const series = this._series;
      const d = pane.data?.digits || 2;

      target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: rx, verticalPixelRatio: ry }) => {
        const cw = ctx.canvas.width;
        const ch = ctx.canvas.height;

        // 1. Session Killzone Bands
        for (const s of this._sessions) {
          const x1 = ts.logicalToCoordinate(s.startIdx);
          const x2 = ts.logicalToCoordinate(s.endIdx + 1);
          if (x1 === null || x2 === null) continue;
          const left = x1 * rx;
          const width = Math.max(4 * rx, (x2 - x1) * rx);
          if (left + width < 0 || left > cw) continue;
          ctx.fillStyle =
            s.sess === "ASIA"
              ? "rgba(168, 85, 247, 0.06)"
              : s.sess === "LONDON"
                ? "rgba(56, 189, 248, 0.06)"
                : "rgba(34, 197, 94, 0.06)";
          ctx.fillRect(left, 0, width, ch);
          ctx.fillStyle = "rgba(148, 163, 184, 0.65)";
          ctx.font = `600 ${Math.round(9.5 * ry)}px ui-sans-serif, system-ui, sans-serif`;
          ctx.textAlign = "left";
          ctx.textBaseline = "top";
          ctx.fillText(s.sess, left + 4 * rx, 4 * ry);
        }

        // 2. Fair Value Gaps (FVG)
        const rightEdgeIdx = Math.max(1, pane.displayCandles.length + 2);
        const fvgRightRaw = ts.logicalToCoordinate(rightEdgeIdx);
        for (const z of this._fvgs) {
          const x1 = ts.logicalToCoordinate(z.startIdx);
          const y1 = series.priceToCoordinate(z.high);
          const y2 = series.priceToCoordinate(z.low);
          if (x1 === null || y1 === null || y2 === null) continue;
          const left = x1 * rx;
          const right = fvgRightRaw !== null ? Math.min(cw, fvgRightRaw * rx) : cw;
          if (right <= left) continue;
          const top = Math.min(y1, y2) * ry;
          const height = Math.max(2 * ry, Math.abs(y2 - y1) * ry);
          ctx.fillStyle = z.dir === "BULL" ? "rgba(34, 197, 94, 0.12)" : "rgba(248, 113, 113, 0.12)";
          ctx.strokeStyle = z.dir === "BULL" ? "rgba(34, 197, 94, 0.45)" : "rgba(248, 113, 113, 0.45)";
          ctx.lineWidth = Math.max(1, rx);
          ctx.fillRect(left, top, right - left, height);
          ctx.strokeRect(left, top, right - left, height);
        }

        // 3. Previous Day High / Low (PDH / PDL)
        if (this._pdhl) {
          const drawLevel = (price, label, color) => {
            const yr = series.priceToCoordinate(price);
            if (yr === null) return;
            const y = yr * ry;
            ctx.save();
            ctx.setLineDash([5 * rx, 4 * rx]);
            ctx.strokeStyle = color;
            ctx.lineWidth = Math.max(1, rx);
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(cw, y);
            ctx.stroke();
            ctx.font = `700 ${Math.round(10 * ry)}px ui-sans-serif, system-ui, sans-serif`;
            ctx.fillStyle = color;
            ctx.textAlign = "right";
            ctx.textBaseline = "bottom";
            ctx.fillText(`${label} ${formatNum(price, d)}`, cw - 8 * rx, y - 3 * ry);
            ctx.restore();
          };
          drawLevel(this._pdhl.pdh, "PDH", "rgba(56, 189, 248, 0.8)");
          drawLevel(this._pdhl.pdl, "PDL", "rgba(245, 158, 11, 0.8)");
        }

        // 4. Bot Live / Forecast / Inspected Position Boxes
        for (const box of this._botBoxes) {
          const x1Raw = ts.logicalToCoordinate(box.startIdx);
          const x2Raw = ts.logicalToCoordinate(box.endIdx);
          const yEntryRaw = series.priceToCoordinate(box.entry);
          const ySlRaw = series.priceToCoordinate(box.sl);
          const yTargetRaw = series.priceToCoordinate(box.target);
          if (x1Raw === null || x2Raw === null || yEntryRaw === null || ySlRaw === null || yTargetRaw === null) continue;
          const left = Math.max(-4, Math.min(x1Raw, x2Raw) * rx);
          const right = Math.min(cw + 4, Math.max(x1Raw, x2Raw) * rx);
          const width = right - left;
          if (width < 4) continue;
          const yEntry = yEntryRaw * ry;
          const ySl = ySlRaw * ry;
          const yTarget = yTargetRaw * ry;

          const slTop = Math.min(yEntry, ySl);
          const slH = Math.max(2 * ry, Math.abs(ySl - yEntry));
          ctx.fillStyle = "rgba(248, 113, 113, 0.16)";
          ctx.fillRect(left, slTop, width, slH);
          ctx.strokeStyle = "rgba(248, 113, 113, 0.8)";
          ctx.lineWidth = Math.max(1, rx);
          ctx.strokeRect(left, slTop, width, slH);

          const tpTop = Math.min(yEntry, yTarget);
          const tpH = Math.max(2 * ry, Math.abs(yTarget - yEntry));
          ctx.fillStyle = "rgba(34, 197, 94, 0.16)";
          ctx.fillRect(left, tpTop, width, tpH);
          ctx.strokeStyle = "rgba(34, 197, 94, 0.8)";
          ctx.strokeRect(left, tpTop, width, tpH);

          ctx.strokeStyle = "#38bdf8";
          ctx.lineWidth = Math.max(1.4, 1.5 * rx);
          ctx.beginPath();
          ctx.moveTo(left, yEntry);
          ctx.lineTo(right, yEntry);
          ctx.stroke();

          const topEdge = Math.max(6 * ry, Math.min(ch - 26 * ry, Math.min(slTop, tpTop)));
          const fontPx = Math.round(10.5 * ry);
          ctx.font = `700 ${fontPx}px ui-sans-serif, system-ui, sans-serif`;
          const textW = ctx.measureText(box.badgeText).width;
          const padX = 6 * rx;
          const badgeH = 19 * ry;
          const badgeW = Math.min(cw - 12 * rx, textW + padX * 2);
          const badgeX = Math.max(6 * rx, Math.min(cw - badgeW - 6 * rx, left + 4 * rx));
          const badgeY = Math.max(4 * ry, topEdge - badgeH - 4 * ry);
          ctx.fillStyle = "rgba(11, 15, 23, 0.9)";
          ctx.fillRect(badgeX, badgeY, badgeW, badgeH);
          ctx.strokeStyle = box.side === "BUY" ? "#22c55e" : "#f87171";
          ctx.strokeRect(badgeX, badgeY, badgeW, badgeH);
          ctx.fillStyle = "#e6ecf7";
          ctx.textAlign = "left";
          ctx.textBaseline = "middle";
          ctx.fillText(box.badgeText, badgeX + padX, badgeY + badgeH / 2, badgeW - padX * 2);
        }

        // 5. User Drawings + Active Draft
        const drawings = studio.drawingsBySymbol[pane.symbol] || [];
        const allItems = this._draft ? [...drawings, this._draft] : drawings;
        for (const item of allItems) {
          const isSelected = studio.selectedDrawing?.symbol === pane.symbol && studio.selectedDrawing?.id === item.id;
          this._drawItem(ctx, rx, ry, cw, ch, d, item, isSelected);
        }

        // 6. Magnet Snap Ring
        if (this._magnetSnap) {
          const sx = ts.logicalToCoordinate(timeToLogical(pane, this._magnetSnap.time));
          const sy = series.priceToCoordinate(this._magnetSnap.price);
          if (sx !== null && sy !== null) {
            ctx.save();
            ctx.strokeStyle = "#38bdf8";
            ctx.lineWidth = Math.max(1.5, 1.8 * rx);
            ctx.beginPath();
            ctx.arc(sx * rx, sy * ry, 5 * rx, 0, Math.PI * 2);
            ctx.stroke();
            ctx.restore();
          }
        }
      });
    }

    _drawHandle(ctx, rx, x, y) {
      ctx.save();
      ctx.fillStyle = "#0e1422";
      ctx.strokeStyle = "#38bdf8";
      ctx.lineWidth = Math.max(1.5, 1.5 * rx);
      ctx.beginPath();
      ctx.arc(x, y, 4.5 * rx, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }

    _drawItem(ctx, rx, ry, cw, ch, digits, item, selected) {
      const pane = this._pane;
      const p1 = getPointPixel(pane, item.p1);
      const p2 = item.p2 ? getPointPixel(pane, item.p2) : null;
      if (!p1) return;
      const x1 = p1.x * rx;
      const y1 = p1.y * ry;
      const x2 = p2 ? p2.x * rx : x1;
      const y2 = p2 ? p2.y * ry : y1;

      const strokeCol = item.color || "#38bdf8";
      ctx.save();
      ctx.strokeStyle = strokeCol;
      ctx.fillStyle = strokeCol;
      ctx.lineWidth = Math.max(1.4, (selected ? 2.2 : 1.7) * rx);

      if (item.type === "trendline") {
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        if (selected) {
          this._drawHandle(ctx, rx, x1, y1);
          this._drawHandle(ctx, rx, x2, y2);
        }
      } else if (item.type === "ray") {
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(cw, y1);
        ctx.stroke();
        this._drawPriceTag(ctx, rx, ry, cw, y1, formatNum(item.p1.price, digits), strokeCol);
        if (selected) this._drawHandle(ctx, rx, x1, y1);
      } else if (item.type === "hline") {
        ctx.beginPath();
        ctx.moveTo(0, y1);
        ctx.lineTo(cw, y1);
        ctx.stroke();
        this._drawPriceTag(ctx, rx, ry, cw, y1, formatNum(item.p1.price, digits), strokeCol);
        if (selected) this._drawHandle(ctx, rx, x1, y1);
      } else if (item.type === "vline") {
        ctx.beginPath();
        ctx.moveTo(x1, 0);
        ctx.lineTo(x1, ch);
        ctx.stroke();
        if (selected) this._drawHandle(ctx, rx, x1, y1);
      } else if (item.type === "rect" && p2) {
        const left = Math.min(x1, x2);
        const top = Math.min(y1, y2);
        const w = Math.abs(x2 - x1);
        const h = Math.abs(y2 - y1);
        ctx.fillStyle = "rgba(56, 189, 248, 0.14)";
        ctx.fillRect(left, top, w, h);
        ctx.strokeRect(left, top, w, h);
        if (selected) {
          this._drawHandle(ctx, rx, x1, y1);
          this._drawHandle(ctx, rx, x2, y2);
        }
      } else if (item.type === "fib" && p2) {
        const left = Math.min(x1, x2);
        const right = Math.max(x1, x2, left + 80 * rx);
        const hiP = item.p1.price;
        const loP = item.p2.price;
        ctx.setLineDash([4 * rx, 3 * rx]);
        ctx.strokeStyle = "rgba(148, 163, 184, 0.7)";
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.font = `600 ${Math.round(10 * ry)}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textAlign = "left";
        ctx.textBaseline = "bottom";
        for (const lvl of FIB_LEVELS) {
          const priceAtLvl = hiP + (loP - hiP) * lvl.r;
          const yr = pane.mainSeries.priceToCoordinate(priceAtLvl);
          if (yr === null) continue;
          const y = yr * ry;
          ctx.strokeStyle = lvl.color;
          ctx.lineWidth = Math.max(1, 1.2 * rx);
          ctx.beginPath();
          ctx.moveTo(left, y);
          ctx.lineTo(right, y);
          ctx.stroke();
          ctx.fillStyle = lvl.color;
          ctx.fillText(`${lvl.r.toFixed(3).replace(/0+$/, "").replace(/\.$/, "")} (${formatNum(priceAtLvl, digits)})`, left + 4 * rx, y - 2 * ry);
        }
        if (selected) {
          this._drawHandle(ctx, rx, x1, y1);
          this._drawHandle(ctx, rx, x2, y2);
        }
      } else if ((item.type === "long" || item.type === "short") && p2) {
        const slYRaw = pane.mainSeries.priceToCoordinate(item.slPrice);
        if (slYRaw !== null) {
          const slY = slYRaw * ry;
          const left = Math.min(x1, x2);
          const right = Math.max(x1, x2, left + 50 * rx);
          const w = right - left;

          // Target Zone (Green)
          const tpTop = Math.min(y1, y2);
          const tpH = Math.max(2 * ry, Math.abs(y2 - y1));
          ctx.fillStyle = "rgba(34, 197, 94, 0.18)";
          ctx.strokeStyle = "rgba(34, 197, 94, 0.85)";
          ctx.fillRect(left, tpTop, w, tpH);
          ctx.strokeRect(left, tpTop, w, tpH);

          // Stop Zone (Red)
          const slTop = Math.min(y1, slY);
          const slH = Math.max(2 * ry, Math.abs(slY - y1));
          ctx.fillStyle = "rgba(248, 113, 113, 0.18)";
          ctx.strokeStyle = "rgba(248, 113, 113, 0.85)";
          ctx.fillRect(left, slTop, w, slH);
          ctx.strokeRect(left, slTop, w, slH);

          // Entry line
          ctx.strokeStyle = "#e2e8f0";
          ctx.beginPath();
          ctx.moveTo(left, y1);
          ctx.lineTo(right, y1);
          ctx.stroke();

          const risk = Math.abs(item.p1.price - item.slPrice);
          const reward = Math.abs(item.p2.price - item.p1.price);
          const rr = risk > 0 ? reward / risk : 0;
          const pct = item.p1.price > 0 ? (reward / item.p1.price) * 100 : 0;
          const label = `${item.type.toUpperCase()} · R:R 1:${rr.toFixed(2)} · TP ${formatNum(item.p2.price, digits)} (+${pct.toFixed(2)}%) · SL ${formatNum(item.slPrice, digits)}`;
          this._drawCallout(ctx, rx, ry, cw, ch, left + 4 * rx, Math.min(tpTop, slTop) - 4 * ry, label, "#22c55e");

          if (selected) {
            this._drawHandle(ctx, rx, x1, y1);
            this._drawHandle(ctx, rx, x2, y2);
            this._drawHandle(ctx, rx, left + w / 2, slY);
          }
        }
      } else if (item.type === "measure" && p2) {
        const left = Math.min(x1, x2);
        const top = Math.min(y1, y2);
        const w = Math.abs(x2 - x1);
        const h = Math.abs(y2 - y1);
        const up = item.p2.price >= item.p1.price;
        ctx.fillStyle = up ? "rgba(34, 197, 94, 0.16)" : "rgba(248, 113, 113, 0.16)";
        ctx.strokeStyle = up ? "#22c55e" : "#f87171";
        ctx.fillRect(left, top, w, h);
        ctx.strokeRect(left, top, w, h);

        const diff = item.p2.price - item.p1.price;
        const pct = item.p1.price !== 0 ? (diff / item.p1.price) * 100 : 0;
        const bars = Math.round(Math.abs(p2.logical - p1.logical));
        const label = `${formatSigned(diff, digits)} (${formatSigned(pct, 2, "%")}) · ${bars} bars`;
        this._drawCallout(ctx, rx, ry, cw, ch, left + w / 2 - 70 * rx, top - 4 * ry, label, up ? "#22c55e" : "#f87171");
      } else if (item.type === "text") {
        const txt = item.text || "Note";
        const fontPx = Math.round(11.5 * ry);
        ctx.font = `600 ${fontPx}px ui-sans-serif, system-ui, sans-serif`;
        const w = ctx.measureText(txt).width + 14 * rx;
        const h = 22 * ry;
        ctx.fillStyle = "rgba(15, 23, 42, 0.92)";
        ctx.strokeStyle = selected ? "#38bdf8" : "#475569";
        ctx.fillRect(x1, y1 - h / 2, w, h);
        ctx.strokeRect(x1, y1 - h / 2, w, h);
        ctx.fillStyle = "#f8fafc";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(txt, x1 + 7 * rx, y1);
      }

      ctx.restore();
    }

    _drawPriceTag(ctx, rx, ry, cw, y, text, color) {
      const fontPx = Math.round(10 * ry);
      ctx.font = `700 ${fontPx}px ui-sans-serif, system-ui, sans-serif`;
      const w = ctx.measureText(text).width + 10 * rx;
      const h = 16 * ry;
      const x = Math.max(4 * rx, cw - w - 6 * rx);
      ctx.fillStyle = color;
      ctx.fillRect(x, y - h / 2, w, h);
      ctx.fillStyle = "#06131f";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, x + w / 2, y);
    }

    _drawCallout(ctx, rx, ry, cw, ch, rawX, rawBottomY, text, borderCol) {
      const fontPx = Math.round(10.5 * ry);
      ctx.font = `700 ${fontPx}px ui-sans-serif, system-ui, sans-serif`;
      const tw = ctx.measureText(text).width;
      const padX = 7 * rx;
      const h = 19 * ry;
      const w = Math.min(cw - 12 * rx, tw + padX * 2);
      const x = Math.max(6 * rx, Math.min(cw - w - 6 * rx, rawX));
      const y = Math.max(4 * ry, Math.min(ch - h - 4 * ry, rawBottomY - h));
      ctx.fillStyle = "rgba(11, 15, 23, 0.92)";
      ctx.fillRect(x, y, w, h);
      ctx.strokeStyle = borderCol;
      ctx.lineWidth = Math.max(1, rx);
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = "#e6ecf7";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(text, x + padX, y + h / 2, w - padX * 2);
    }
  }

  // ===== Drawing Hit-Testing & Pointer Interaction =====
  function hitTestDrawings(pane, cssX, cssY) {
    const drawings = studio.drawingsBySymbol[pane.symbol] || [];
    for (let i = drawings.length - 1; i >= 0; i -= 1) {
      const item = drawings[i];
      const p1 = getPointPixel(pane, item.p1);
      if (!p1) continue;
      const p2 = item.p2 ? getPointPixel(pane, item.p2) : null;

      if (Math.hypot(cssX - p1.x, cssY - p1.y) <= 9) return { item, part: "p1" };
      if (p2 && Math.hypot(cssX - p2.x, cssY - p2.y) <= 9) return { item, part: "p2" };

      if ((item.type === "long" || item.type === "short") && p2) {
        const slY = pane.mainSeries.priceToCoordinate(item.slPrice);
        const midX = (Math.min(p1.x, p2.x) + Math.max(p1.x, p2.x, Math.min(p1.x, p2.x) + 50)) / 2;
        if (slY !== null && Math.hypot(cssX - midX, cssY - slY) <= 9) return { item, part: "sl" };
        const left = Math.min(p1.x, p2.x);
        const right = Math.max(p1.x, p2.x, left + 50);
        const top = Math.min(p1.y, p2.y, slY ?? p1.y);
        const bot = Math.max(p1.y, p2.y, slY ?? p1.y);
        if (cssX >= left && cssX <= right && cssY >= top && cssY <= bot) return { item, part: "body" };
      } else if (item.type === "trendline" && p2) {
        if (distToSegment(cssX, cssY, p1.x, p1.y, p2.x, p2.y) <= 7) return { item, part: "body" };
      } else if (item.type === "hline") {
        if (Math.abs(cssY - p1.y) <= 6) return { item, part: "body" };
      } else if (item.type === "ray") {
        if (cssX >= p1.x - 4 && Math.abs(cssY - p1.y) <= 6) return { item, part: "body" };
      } else if (item.type === "vline") {
        if (Math.abs(cssX - p1.x) <= 6) return { item, part: "body" };
      } else if ((item.type === "rect" || item.type === "fib" || item.type === "measure") && p2) {
        const left = Math.min(p1.x, p2.x);
        const right = Math.max(p1.x, p2.x);
        const top = Math.min(p1.y, p2.y);
        const bot = Math.max(p1.y, p2.y);
        if (cssX >= left - 4 && cssX <= right + 4 && cssY >= top - 4 && cssY <= bot + 4) return { item, part: "body" };
      } else if (item.type === "text") {
        if (cssX >= p1.x - 4 && cssX <= p1.x + 120 && Math.abs(cssY - p1.y) <= 14) return { item, part: "body" };
      }
    }
    return null;
  }

  function pushUndo(symbol) {
    const prev = JSON.parse(JSON.stringify(studio.drawingsBySymbol[symbol] || []));
    studio.undoStack.push({ symbol, list: prev });
    if (studio.undoStack.length > 40) studio.undoStack.shift();
  }

  function persistDrawings() {
    saveJsonStorage(STORAGE_KEY_DRAWINGS, studio.drawingsBySymbol);
  }

  function addDrawing(symbol, item) {
    pushUndo(symbol);
    if (!studio.drawingsBySymbol[symbol]) studio.drawingsBySymbol[symbol] = [];
    studio.drawingsBySymbol[symbol].push(item);
    studio.selectedDrawing = { symbol, id: item.id };
    persistDrawings();
    redrawAllPanes();
  }

  function deleteSelectedDrawing() {
    const sel = studio.selectedDrawing;
    if (!sel) return;
    const list = studio.drawingsBySymbol[sel.symbol];
    if (!list) return;
    pushUndo(sel.symbol);
    studio.drawingsBySymbol[sel.symbol] = list.filter((d) => d.id !== sel.id);
    studio.selectedDrawing = null;
    persistDrawings();
    redrawAllPanes();
    announceStatus("Deleted selected drawing.");
  }

  function undoDrawing() {
    const snap = studio.undoStack.pop();
    if (!snap) return;
    studio.drawingsBySymbol[snap.symbol] = snap.list;
    studio.selectedDrawing = null;
    persistDrawings();
    redrawAllPanes();
    announceStatus("Undid drawing change.");
  }

  function clearSymbolDrawings() {
    const pane = studio.panes[studio.activePaneIdx];
    if (!pane) return;
    pushUndo(pane.symbol);
    studio.drawingsBySymbol[pane.symbol] = [];
    studio.selectedDrawing = null;
    persistDrawings();
    redrawAllPanes();
    announceStatus(`Cleared all drawings on ${pane.symbol}.`);
  }

  function setActiveTool(tool) {
    studio.tool = tool;
    for (const btn of document.querySelectorAll(".tv-tool-btn[data-tool]")) {
      btn.setAttribute("aria-pressed", String(btn.dataset.tool === tool));
    }
    const hint = $("tv-draw-hint");
    if (hint) {
      hint.textContent =
        tool === "cursor"
          ? "Cursor active · Drag drawings to move · Shift+Drag or Alt+M to measure · Del removes selected"
          : `Tool: ${tool.toUpperCase()} · Click on chart to place (Esc to cancel)`;
    }
    for (const p of studio.panes) {
      if (p.canvasHost) {
        p.canvasHost.style.cursor = tool === "cursor" ? "crosshair" : "cell";
      }
      if (p.chart) {
        const allowScroll = tool === "cursor";
        p.chart.applyOptions({
          handleScroll: { mouseWheel: true, pressedMouseMove: allowScroll, horzTouchDrag: allowScroll, vertTouchDrag: allowScroll },
          handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
        });
      }
    }
  }

  function attachPanePointerController(pane) {
    const host = pane.canvasHost;
    if (!host) return;
    let draft = null;
    let dragState = null;

    host.addEventListener("pointerdown", (e) => {
      setActivePane(pane.idx);
      if (e.button === 2) return;
      const rect = host.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;

      // Replay pick mode takes precedence
      if (studio.replay.active && studio.replay.picking) {
        const pt = pixelToDataPoint(pane, x, y, false);
        if (pt && pane.rawCandles.length) {
          const idx = Math.max(10, Math.min(pane.rawCandles.length - 1, findBarIndex(pane.rawCandles, pt.time)));
          studio.replay.cutoffIdx = idx;
          studio.replay.picking = false;
          applyReplaySlice(pane, true);
          announceStatus(`Replay positioned at bar ${idx + 1}/${pane.rawCandles.length}`);
        }
        return;
      }

      const activeTool = e.shiftKey && studio.tool === "cursor" ? "measure" : studio.tool;
      const pt = pixelToDataPoint(pane, x, y, studio.magnet);
      if (!pt) return;

      if (activeTool === "cursor") {
        const hit = !studio.locked ? hitTestDrawings(pane, x, y) : null;
        if (hit) {
          studio.selectedDrawing = { symbol: pane.symbol, id: hit.item.id };
          pushUndo(pane.symbol);
          dragState = {
            item: hit.item,
            part: hit.part,
            startPt: pt,
            orig: JSON.parse(JSON.stringify(hit.item)),
          };
          pane.chart.applyOptions({ handleScroll: { pressedMouseMove: false } });
          host.setPointerCapture(e.pointerId);
          pane.primitive?.requestRedraw();
          e.stopPropagation();
        } else {
          studio.selectedDrawing = null;
          pane.primitive?.requestRedraw();
        }
        return;
      }

      // 1-click placement tools
      const id = `d_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
      if (activeTool === "hline" || activeTool === "ray" || activeTool === "vline") {
        addDrawing(pane.symbol, { id, type: activeTool, p1: pt, color: "#38bdf8" });
        setActiveTool("cursor");
        return;
      }
      if (activeTool === "text") {
        const note = window.prompt("Chart annotation text:", "Key Level");
        if (note && note.trim()) {
          addDrawing(pane.symbol, { id, type: "text", p1: pt, text: note.trim(), color: "#e2e8f0" });
        }
        setActiveTool("cursor");
        return;
      }
      if (activeTool === "long" || activeTool === "short") {
        const candles = pane.displayCandles;
        const atrList = computeATR(candles, 14);
        const atrVal = atrList.length ? atrList[atrList.length - 1].value : pt.price * 0.008;
        const slDist = Math.max(pt.price * 0.002, atrVal * 1.5);
        const isLong = activeTool === "long";
        const slPrice = isLong ? pt.price - slDist : pt.price + slDist;
        const tpPrice = isLong ? pt.price + slDist * 2 : pt.price - slDist * 2;
        const endTime = logicalToTime(pane, pt.logical + 14);
        addDrawing(pane.symbol, {
          id,
          type: activeTool,
          p1: pt,
          p2: { time: endTime, price: tpPrice },
          slPrice,
        });
        setActiveTool("cursor");
        return;
      }

      // 2-point drag placement tools: trendline, rect, fib, measure
      draft = { id, type: activeTool, p1: pt, p2: pt, color: activeTool === "fib" ? "#f59e0b" : "#38bdf8" };
      pane.primitive?.setDraft(draft);
      host.setPointerCapture(e.pointerId);
      e.stopPropagation();
    });

    host.addEventListener("pointermove", (e) => {
      const rect = host.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;

      if (studio.magnet && (draft || dragState || studio.tool !== "cursor")) {
        const snap = pixelToDataPoint(pane, x, y, true);
        pane.primitive?.setMagnetSnap(snap);
      } else {
        pane.primitive?.setMagnetSnap(null);
      }

      if (draft) {
        const pt = pixelToDataPoint(pane, x, y, studio.magnet);
        if (pt) {
          draft.p2 = pt;
          pane.primitive?.setDraft(draft);
        }
        return;
      }

      if (dragState) {
        const pt = pixelToDataPoint(pane, x, y, studio.magnet);
        if (!pt) return;
        const { item, part, startPt, orig } = dragState;
        if (part === "p1") {
          item.p1 = pt;
        } else if (part === "p2") {
          item.p2 = pt;
        } else if (part === "sl") {
          item.slPrice = pt.price;
        } else if (part === "body") {
          const dTime = pt.time - startPt.time;
          const dPrice = pt.price - startPt.price;
          item.p1 = { time: orig.p1.time + dTime, price: orig.p1.price + dPrice };
          if (orig.p2) item.p2 = { time: orig.p2.time + dTime, price: orig.p2.price + dPrice };
          if (typeof orig.slPrice === "number") item.slPrice = orig.slPrice + dPrice;
        }
        pane.primitive?.requestRedraw();
      }
    });

    const finishPointer = (e) => {
      if (draft) {
        const pt = pixelToDataPoint(pane, e.clientX - host.getBoundingClientRect().left, e.clientY - host.getBoundingClientRect().top, studio.magnet);
        if (pt) draft.p2 = pt;
        pane.primitive?.setDraft(null);
        const completed = draft;
        draft = null;
        if (completed.type !== "measure") {
          addDrawing(pane.symbol, completed);
          setActiveTool("cursor");
        } else {
          // Keep measure box transiently on symbol until cleared or Esc
          addDrawing(pane.symbol, completed);
          setActiveTool("cursor");
        }
      }
      if (dragState) {
        dragState = null;
        persistDrawings();
        pane.chart.applyOptions({ handleScroll: { pressedMouseMove: studio.tool === "cursor" } });
      }
    };

    host.addEventListener("pointerup", finishPointer);
    host.addEventListener("pointercancel", finishPointer);
    host.addEventListener("pointerup", () => syncAutoScaleIndicator(pane));
    host.addEventListener("dblclick", () => syncAutoScaleIndicator(pane));
    host.addEventListener("wheel", () => syncAutoScaleIndicator(pane), { passive: true });
    host.addEventListener("contextmenu", (e) => {
      const rect = host.getBoundingClientRect();
      const hit = hitTestDrawings(pane, e.clientX - rect.left, e.clientY - rect.top);
      if (hit) {
        e.preventDefault();
        studio.selectedDrawing = { symbol: pane.symbol, id: hit.item.id };
        deleteSelectedDrawing();
      }
    });
  }

  // ===== Pane Lifecycle & Multi-Pane Chart Rendering =====
  function buildPaneDom(pane) {
    const wrap = document.createElement("div");
    wrap.className = `tv-pane${pane.idx === studio.activePaneIdx ? " is-active" : ""}`;
    wrap.dataset.paneIdx = String(pane.idx);

    const legend = document.createElement("div");
    legend.className = "tv-pane-legend";
    const mainRow = document.createElement("div");
    mainRow.className = "tv-legend-main";
    const indsRow = document.createElement("div");
    indsRow.className = "tv-legend-inds";
    legend.append(mainRow, indsRow);

    const canvasHost = document.createElement("div");
    canvasHost.className = "tv-pane-canvas";

    wrap.append(legend, canvasHost);
    pane.el = wrap;
    pane.canvasHost = canvasHost;
    pane.legendMainEl = mainRow;
    pane.legendIndsEl = indsRow;
    return wrap;
  }

  function ensurePaneChart(pane) {
    if (pane.chart || typeof window.LightweightCharts === "undefined") return;
    const LWC = window.LightweightCharts;
    const rect = pane.canvasHost.getBoundingClientRect();
    const axisFmt = getAxisFormatter();

    const chart = LWC.createChart(pane.canvasHost, {
      width: Math.max(240, Math.round(rect.width || 600)),
      height: Math.max(240, Math.round(rect.height || 460)),
      layout: {
        background: { type: LWC.ColorType.Solid, color: "#0e1422" },
        textColor: "#93a3c0",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "#192235" },
        horzLines: { color: "#192235" },
      },
      crosshair: { mode: LWC.CrosshairMode.Normal },
      rightPriceScale: {
        borderColor: "#26324d",
        autoScale: studio.scaleAuto,
        invertScale: studio.scaleInv,
        mode: studio.scalePct
          ? LWC.PriceScaleMode.Percentage
          : studio.scaleLog
            ? LWC.PriceScaleMode.Logarithmic
            : LWC.PriceScaleMode.Normal,
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 12,
        borderColor: "#26324d",
        tickMarkFormatter: (t) => axisFmt.format(new Date(Number(t) * 1000)),
      },
      localization: {
        timeFormatter: (t) => getAxisFormatter().format(new Date(Number(t) * 1000)),
      },
    });

    pane.chart = chart;
    pane.primitive = new TVOverlayPrimitive(pane);
    createMainSeriesForStyle(pane);

    chart.subscribeCrosshairMove((param) => {
      updatePaneLegend(pane, param);
      if (studio.syncCrosshair && param?.time && studio.layout > 1) {
        const candle = pane.mainSeries ? param.seriesData?.get(pane.mainSeries) : null;
        const pVal = candle ? (candle.close ?? candle.value) : null;
        if (pVal != null) {
          for (const other of studio.panes.slice(0, studio.layout)) {
            if (other !== pane && other.chart && other.mainSeries) {
              try {
                other.chart.setCrosshairPosition(pVal, param.time, other.mainSeries);
              } catch (_) {
                // Ignore if timestamps don't overlap
              }
            }
          }
        }
      }
    });

    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver((entries) => {
        const entry = entries[0];
        if (!entry || !pane.chart) return;
        const w = Math.floor(entry.contentRect.width);
        const h = Math.floor(entry.contentRect.height);
        if (w > 40 && h > 40) pane.chart.applyOptions({ width: w, height: h });
      });
      ro.observe(pane.canvasHost);
      pane.ro = ro;
    }

    attachPanePointerController(pane);
  }

  function createMainSeriesForStyle(pane) {
    const LWC = window.LightweightCharts;
    if (!pane.chart) return;
    if (pane.mainSeries) {
      if (pane.primitive) {
        try {
          pane.mainSeries.detachPrimitive(pane.primitive);
        } catch (_) {}
      }
      pane.chart.removeSeries(pane.mainSeries);
      pane.mainSeries = null;
      pane.markersApi = null;
    }

    const style = studio.chartStyle;
    let s;
    if (style === "bars") {
      s = pane.chart.addSeries(LWC.BarSeries, { upColor: "#22c55e", downColor: "#f87171" });
    } else if (style === "line") {
      s = pane.chart.addSeries(LWC.LineSeries, { color: "#38bdf8", lineWidth: 2 });
    } else if (style === "area") {
      s = pane.chart.addSeries(LWC.AreaSeries, {
        lineColor: "#38bdf8",
        topColor: "rgba(56, 189, 248, 0.32)",
        bottomColor: "rgba(56, 189, 248, 0.02)",
        lineWidth: 2,
      });
    } else if (style === "hollow") {
      s = pane.chart.addSeries(LWC.CandlestickSeries, {
        upColor: "transparent",
        downColor: "#f87171",
        borderUpColor: "#22c55e",
        borderDownColor: "#f87171",
        wickUpColor: "#22c55e",
        wickDownColor: "#f87171",
      });
    } else {
      s = pane.chart.addSeries(LWC.CandlestickSeries, {
        upColor: "#22c55e",
        downColor: "#f87171",
        borderVisible: false,
        wickUpColor: "#22c55e",
        wickDownColor: "#f87171",
      });
    }

    pane.mainSeries = s;
    pane.markersApi = LWC.createSeriesMarkers(s, []);
    if (pane.primitive) s.attachPrimitive(pane.primitive);
  }

  function syncOverlaySeries(pane, candles, digits) {
    const LWC = window.LightweightCharts;
    const minMove = Number((1 / Math.pow(10, digits)).toFixed(digits));
    const priceFormat = { type: "price", precision: digits, minMove };
    const ind = studio.indicators;

    const ensureLine = (key, color, dashed = false, width = 1.4) => {
      if (!pane.overlaySeries[key]) {
        pane.overlaySeries[key] = pane.chart.addSeries(LWC.LineSeries, {
          color,
          lineWidth: width,
          lineStyle: dashed ? LWC.LineStyle.Dashed : LWC.LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          priceFormat,
        });
      } else {
        pane.overlaySeries[key].applyOptions({ priceFormat });
      }
      return pane.overlaySeries[key];
    };

    const setLine = (key, enabled, pts, color, dashed = false, width = 1.4) => {
      if (enabled && pts.length) {
        ensureLine(key, color, dashed, width).setData(pts);
      } else if (pane.overlaySeries[key]) {
        pane.overlaySeries[key].setData([]);
      }
    };

    setLine("ema20", ind.ema20, ind.ema20 ? computeEMA(candles, 20) : [], "#38bdf8");
    setLine("ema50", ind.ema50, ind.ema50 ? computeEMA(candles, 50) : [], "#f59e0b");
    setLine("ema200", ind.ema200, ind.ema200 ? computeEMA(candles, 200) : [], "#a855f7");
    setLine("sma20", ind.sma20, ind.sma20 ? computeSMA(candles, 20) : [], "#94a3b8");
    setLine("vwap", ind.vwap, ind.vwap ? computeVWAP(candles, 24) : [], "#ec4899");

    const don = ind.donchian ? computeDonchian(candles, 20) : { hi: [], lo: [] };
    setLine("don_hi", ind.donchian, don.hi, "#22c55e", true, 1.3);
    setLine("don_lo", ind.donchian, don.lo, "#f87171", true, 1.3);

    const bb = ind.bb ? computeBollinger(candles, 20, 2) : { upper: [], basis: [], lower: [] };
    setLine("bb_u", ind.bb, bb.upper, "#60a5fa", false, 1.1);
    setLine("bb_b", ind.bb, bb.basis, "#93c5fd", true, 1.0);
    setLine("bb_l", ind.bb, bb.lower, "#60a5fa", false, 1.1);

    // Sub-pane oscillators using LightweightCharts v5 addPane()
    const syncSubPane = (key, enabled, buildFn) => {
      if (!enabled) {
        if (pane.subPanes[key]) {
          const allPanes = pane.chart.panes();
          const pIdx = allPanes.indexOf(pane.subPanes[key].pane);
          if (pIdx > 0) pane.chart.removePane(pIdx);
          delete pane.subPanes[key];
        }
        return;
      }
      if (!pane.subPanes[key]) {
        const sub = pane.chart.addPane();
        sub.setHeight(key === "volume" ? 85 : 115);
        pane.subPanes[key] = { pane: sub, series: buildFn(sub, LWC) };
      }
      return pane.subPanes[key];
    };

    const volPane = syncSubPane("volume", ind.volume, (sub, LWC) => ({
      hist: sub.addSeries(LWC.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false }),
    }));
    if (volPane) {
      volPane.series.hist.setData(
        candles.map((c) => ({
          time: c.time,
          value: Number(c.volume || 0),
          color: c.close >= c.open ? "rgba(34, 197, 94, 0.45)" : "rgba(248, 113, 113, 0.45)",
        }))
      );
    }

    const rsiPane = syncSubPane("rsi", ind.rsi, (sub, LWC) => {
      const line = sub.addSeries(LWC.LineSeries, { color: "#a855f7", lineWidth: 1.6 });
      line.createPriceLine({ price: 70, color: "rgba(248,113,113,0.45)", lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
      line.createPriceLine({ price: 30, color: "rgba(34,197,94,0.45)", lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
      return { line };
    });
    if (rsiPane) rsiPane.series.line.setData(computeRSI(candles, 14));

    const macdPane = syncSubPane("macd", ind.macd, (sub, LWC) => ({
      hist: sub.addSeries(LWC.HistogramSeries, { priceLineVisible: false }),
      macd: sub.addSeries(LWC.LineSeries, { color: "#38bdf8", lineWidth: 1.5, priceLineVisible: false }),
      sig: sub.addSeries(LWC.LineSeries, { color: "#f59e0b", lineWidth: 1.4, priceLineVisible: false }),
    }));
    if (macdPane) {
      const m = computeMACD(candles);
      macdPane.series.hist.setData(m.hist);
      macdPane.series.macd.setData(m.macd);
      macdPane.series.sig.setData(m.signal);
    }

    const atrPane = syncSubPane("atr", ind.atr, (sub, LWC) => ({
      line: sub.addSeries(LWC.LineSeries, { color: "#f59e0b", lineWidth: 1.5, priceFormat }),
    }));
    if (atrPane) {
      atrPane.series.line.applyOptions({ priceFormat });
      atrPane.series.line.setData(computeATR(candles, 14));
    }
  }

  function buildBotOverlayBoxes(pane, candles) {
    if (!studio.showBotBoxes || !candles.length) return { boxes: [], cards: [] };
    const snap = studio.snapshotRef;
    const digits = pane.data?.digits || 2;
    const minMove = Number((1 / Math.pow(10, digits)).toFixed(digits));
    const fallbackSlDist = candles[candles.length - 1].close * 0.006;
    const rr = studio.targetR || 3;

    const matchesPaneSymbol = (sym) => {
      const s = String(sym || "").toUpperCase();
      return s === pane.symbol || SYMBOL_ALIAS_TO_TV[s] === pane.symbol;
    };

    const boxes = [];
    const cards = [];
    const openPos = (snap?.positions || []).filter((p) => matchesPaneSymbol(p.symbol));
    for (const p of openPos) {
      const startIdx = findBarIndex(candles, p.open_epoch || candles[candles.length - 1].time);
      const endIdx = Math.max(startIdx + 6, candles.length + 5);
      const entry = Number(p.price_open);
      const sl = Number(p.sl) > 0 ? Number(p.sl) : p.direction === "BUY" ? entry - fallbackSlDist : entry + fallbackSlDist;
      const riskDist = Math.max(minMove, Math.abs(entry - sl));
      const projTarget = p.direction === "BUY" ? entry + rr * riskDist : entry - rr * riskDist;
      const nowPrice = Number(p.price_current || entry);
      const target = p.direction === "BUY" ? Math.max(projTarget, nowPrice) : Math.min(projTarget, nowPrice);
      const rNow = p.direction === "BUY" ? (nowPrice - entry) / riskDist : (entry - nowPrice) / riskDist;
      boxes.push({
        side: p.direction,
        startIdx,
        endIdx,
        entry,
        sl,
        target,
        badgeText: `LIVE ${p.direction} · ${rNow >= 0 ? "+" : ""}${formatNum(rNow, 2)}R · ${formatNum(p.volume, 2)}L`,
      });
      cards.push({
        title: `Live ${p.symbol} ${p.direction}`,
        tag: p.is_bot ? "LIVE BOT" : "LIVE MANUAL",
        tagClass: "green",
        pnl: formatSigned(p.profit, 2),
        pnlClass: Number(p.profit) >= 0 ? "pos" : "neg",
        rows: [
          `Entry: ${formatNum(entry, digits)}`,
          `Current: ${formatNum(nowPrice, digits)}`,
          `SL: ${Number(p.sl) > 0 ? formatNum(p.sl, digits) : "Unset"}`,
          `${rr}R Target: ${formatNum(projTarget, digits)}`,
          `Lots: ${formatNum(p.volume, 2)}`,
          `Live R: ${rNow >= 0 ? "+" : ""}${formatNum(rNow, 2)}R`,
        ],
      });
    }

    const insp = studio.inspectedTrade;
    const clearBtn = $("clear-inspected-trade-btn");
    if (insp && matchesPaneSymbol(insp.symbol)) {
      const startIdx = findBarIndex(candles, insp.entry_epoch);
      const endIdx = Math.max(startIdx + 3, findBarIndex(candles, insp.exit_epoch));
      const entry = Number(insp.entry_price);
      const exitP = Number(insp.exit_price);
      const net = Number(insp.net_pnl || 0);
      const sl = net < 0 ? exitP : insp.direction === "BUY" ? entry - fallbackSlDist : entry + fallbackSlDist;
      const target = net >= 0 ? exitP : insp.direction === "BUY" ? entry + rr * Math.abs(entry - sl) : entry - rr * Math.abs(entry - sl);
      boxes.push({
        side: insp.direction,
        startIdx,
        endIdx,
        entry,
        sl,
        target,
        badgeText: `CLOSED ${insp.direction} · $${formatSigned(net, 2)}`,
      });
      cards.push({
        title: `Inspected ${insp.symbol} ${insp.direction}`,
        tag: insp.is_bot ? "CLOSED BOT" : "CLOSED MANUAL",
        tagClass: "accent",
        pnl: `$${formatSigned(net, 2)}`,
        pnlClass: net >= 0 ? "pos" : "neg",
        rows: [
          `Entry: ${formatNum(entry, digits)}`,
          `Exit: ${formatNum(exitP, digits)}`,
          `Opened: ${insp.entry_time_ny}`,
          `Closed: ${insp.exit_time_ny}`,
          `Lots: ${formatNum(insp.volume, 2)}`,
          `Duration: ${formatNum(insp.duration_hours, 1)}h`,
        ],
      });
      if (clearBtn) clearBtn.hidden = false;
    } else if (clearBtn) {
      clearBtn.hidden = true;
    }

    return { boxes, cards };
  }

  function renderBotPositionCards(cards) {
    const container = $("chart-position-cards");
    if (!container) return;
    container.textContent = "";
    if (!cards.length) {
      const p = document.createElement("p");
      p.className = "empty-note";
      p.textContent = "No open MT5 positions or inspected closed trades for this symbol.";
      container.append(p);
      return;
    }
    for (const c of cards) {
      const card = document.createElement("div");
      card.className = "pos-card";
      const head = document.createElement("div");
      head.className = "pos-card-head";
      const left = document.createElement("div");
      left.className = "brand-title-row";
      const title = document.createElement("strong");
      title.textContent = c.title;
      const pill = document.createElement("span");
      pill.className = `pill ${c.tagClass}`;
      pill.textContent = c.tag;
      left.append(title, pill);
      const pnlEl = document.createElement("strong");
      pnlEl.className = c.pnlClass;
      pnlEl.textContent = c.pnl;
      head.append(left, pnlEl);

      const grid = document.createElement("div");
      grid.className = "pos-metrics";
      for (const r of c.rows) {
        const d = document.createElement("div");
        d.textContent = r;
        grid.append(d);
      }
      card.append(head, grid);
      container.append(card);
    }
  }

  function updatePaneLegend(pane, crosshairParam = null) {
    if (!pane.legendMainEl || !pane.legendIndsEl) return;
    const candles = pane.displayCandles;
    if (!candles.length) {
      pane.legendMainEl.textContent = pane.data?.error
        ? `${pane.symbol} · ${pane.tf.toUpperCase()} · ${pane.data.error}`
        : `${pane.symbol} · ${pane.tf.toUpperCase()} · ${pane._loading ? "Loading..." : "Waiting for data"}`;
      pane.legendIndsEl.textContent = "";
      return;
    }
    const d = pane.data?.digits || 2;
    let c = candles[candles.length - 1];
    if (crosshairParam && pane.mainSeries) {
      const pt = crosshairParam.seriesData?.get(pane.mainSeries);
      if (pt) {
        if (typeof pt.open === "number") c = pt;
        else if (typeof pt.value === "number") c = { open: pt.value, high: pt.value, low: pt.value, close: pt.value, time: pt.time };
      }
    }

    const chg = c.close - c.open;
    const pct = c.open !== 0 ? (chg / c.open) * 100 : 0;
    pane.legendMainEl.textContent = "";
    const titleSpan = document.createElement("span");
    titleSpan.className = "tv-legend-title";
    titleSpan.textContent = `${pane.symbol} · ${pane.tf.toUpperCase()} · ${pane.data?.source || ""}`;

    const ohlcSpan = document.createElement("span");
    ohlcSpan.className = "tv-legend-ohlc";
    ohlcSpan.textContent = `O ${formatNum(c.open, d)}  H ${formatNum(c.high, d)}  L ${formatNum(c.low, d)}  C ${formatNum(c.close, d)}  (${formatSigned(pct, 2, "%")})`;
    ohlcSpan.style.color = chg >= 0 ? "#86efac" : "#fca5a5";
    pane.legendMainEl.append(titleSpan, ohlcSpan);

    pane.legendIndsEl.textContent = "";
    const addIndPill = (label, color) => {
      const pill = document.createElement("span");
      pill.className = "tv-ind-pill";
      const dot = document.createElement("i");
      dot.className = "tv-ind-dot";
      dot.style.background = color;
      pill.append(dot, document.createTextNode(label));
      pane.legendIndsEl.append(pill);
    };
    if (studio.indicators.ema20) addIndPill("EMA 20", "#38bdf8");
    if (studio.indicators.ema50) addIndPill("EMA 50", "#f59e0b");
    if (studio.indicators.ema200) addIndPill("EMA 200", "#a855f7");
    if (studio.indicators.donchian) addIndPill("Donchian 20", "#22c55e");
    if (studio.indicators.bb) addIndPill("BB 20", "#60a5fa");
    if (studio.indicators.vwap) addIndPill("VWAP", "#ec4899");
    if (studio.indicators.fvg) addIndPill("ICT FVG", "#38bdf8");
    if (studio.indicators.sessions) addIndPill("Sessions", "#94a3b8");
  }

  function syncAutoScaleIndicator(pane) {
    if (!pane || pane.idx !== studio.activePaneIdx || !pane.mainSeries) return;
    try {
      const actual = pane.mainSeries.priceScale().options().autoScale;
      if (typeof actual === "boolean" && actual !== studio.scaleAuto) {
        studio.scaleAuto = actual;
        syncScaleAndIndicatorUI();
      }
    } catch (_) {}
  }

  function resetPanePriceScale(pane) {
    if (!pane?.chart) return;
    studio.scaleAuto = true;
    try {
      pane.chart.priceScale("right").applyOptions({ autoScale: true });
      pane.mainSeries?.priceScale().setAutoScale(true);
      for (const sp of Object.values(pane.subPanes || {})) {
        sp.pane?.priceScale("right")?.applyOptions({ autoScale: true });
      }
    } catch (_) {}
    syncScaleAndIndicatorUI();
  }

  function renderPaneData(pane, fitView = false) {
    ensurePaneChart(pane);
    if (!pane.chart || !pane.data) return;

    const symbolChanged = pane._renderedSymbol !== pane.symbol;
    pane._renderedSymbol = pane.symbol;
    const shouldFit = fitView || symbolChanged;

    const raw = pane.data.candles || [];
    pane.rawCandles = raw;
    let slice = raw;
    if (studio.replay.active && pane.idx === studio.activePaneIdx && studio.replay.cutoffIdx !== null) {
      slice = raw.slice(0, Math.min(raw.length, studio.replay.cutoffIdx + 1));
    }

    const display = studio.chartStyle === "heikin" ? computeHeikinAshi(slice) : slice;
    pane.displayCandles = display;
    pane.barStepSec = inferBarStepSec(display);

    const d = pane.data.digits || 2;
    const minMove = Number((1 / Math.pow(10, d)).toFixed(d));
    const priceFormat = { type: "price", precision: d, minMove };
    pane.mainSeries.applyOptions({ priceFormat });

    if (studio.chartStyle === "line" || studio.chartStyle === "area") {
      pane.mainSeries.setData(display.map((c) => ({ time: c.time, value: c.close })));
    } else {
      pane.mainSeries.setData(display);
    }

    syncOverlaySeries(pane, display, d);

    // Plot closed trade markers matching this symbol
    const snap = studio.snapshotRef;
    const matchesPaneSymbol = (sym) => {
      const s = String(sym || "").toUpperCase();
      return s === pane.symbol || SYMBOL_ALIAS_TO_TV[s] === pane.symbol;
    };
    const markers = [];
    if (display.length && studio.showBotBoxes) {
      const firstTs = display[0].time;
      for (const t of (snap?.trades || []).filter((tr) => matchesPaneSymbol(tr.symbol))) {
        if (t.entry_epoch >= firstTs) {
          markers.push({
            time: display[findBarIndex(display, t.entry_epoch)].time,
            position: t.direction === "BUY" ? "belowBar" : "aboveBar",
            color: t.direction === "BUY" ? "#22c55e" : "#f87171",
            shape: t.direction === "BUY" ? "arrowUp" : "arrowDown",
            text: `${t.direction} ${formatNum(t.volume, 2)}`,
          });
        }
      }
      markers.sort((a, b) => a.time - b.time);
    }
    pane.markersApi?.setMarkers(markers);

    const botInfo = buildBotOverlayBoxes(pane, display);
    if (pane.idx === studio.activePaneIdx) renderBotPositionCards(botInfo.cards);

    pane.primitive?.setOverlays({
      botBoxes: botInfo.boxes,
      fvgs: studio.indicators.fvg ? computeFVGs(display) : [],
      sessions: studio.indicators.sessions ? computeSessions(display, pane.tf) : [],
      pdhl: studio.indicators.pdhl ? computePDHL(display) : null,
    });

    updatePaneLegend(pane);

    if (shouldFit && display.length) {
      const from = Math.max(0, display.length - 120);
      pane.chart.timeScale().setVisibleLogicalRange({ from, to: display.length + 10 });
      resetPanePriceScale(pane);
    } else if (symbolChanged) {
      resetPanePriceScale(pane);
    }
  }

  function redrawAllPanes() {
    for (const p of studio.panes.slice(0, studio.layout)) {
      if (p.primitive) p.primitive.requestRedraw();
    }
  }

  async function loadPaneChart(pane, fitView = false) {
    if (!studio.apiJsonFn) return;
    ensurePaneChart(pane);
    if (pane._abort) {
      try {
        pane._abort.abort();
      } catch (_) {}
    }
    const ctrl = new AbortController();
    pane._abort = ctrl;
    const timer = window.setTimeout(() => ctrl.abort(), 15000);
    const reqSeq = (pane._reqSeq || 0) + 1;
    pane._reqSeq = reqSeq;
    const reqSymbol = pane.symbol;
    pane._loading = true;
    try {
      const data = await studio.apiJsonFn(
        `/api/tv/chart?symbol=${encodeURIComponent(reqSymbol)}&tf=${encodeURIComponent(pane.tf)}&bars=600`,
        { signal: ctrl.signal }
      );
      if (reqSeq !== pane._reqSeq || pane.symbol !== reqSymbol) return;
      pane._loading = false;
      pane.data = data;
      renderPaneData(pane, fitView);
      if (pane.idx === studio.activePaneIdx) syncTopToolbarWithActivePane();
    } catch (err) {
      if (reqSeq !== pane._reqSeq) return;
      pane._loading = false;
      if (err.name === "AbortError") {
        if (pane.legendMainEl && !pane.displayCandles?.length) {
          pane.legendMainEl.textContent = `${pane.symbol}: Request timed out (click symbol to retry)`;
        }
        return;
      }
      if (err.message !== "Unauthorized" && pane.legendMainEl) {
        pane.legendMainEl.textContent = `${pane.symbol}: ${err.message}`;
      }
    } finally {
      window.clearTimeout(timer);
      if (pane._abort === ctrl) pane._abort = null;
    }
  }

  function applyLayout(nextLayout) {
    studio.layout = nextLayout;
    if (studio.activePaneIdx >= nextLayout) studio.activePaneIdx = 0;
    const stage = $("tv-stage");
    const tvStudio = $("tv-studio");
    if (stage) stage.dataset.panes = String(nextLayout);
    if (tvStudio) tvStudio.dataset.layout = String(nextLayout);

    // Tear down panes outside new layout count
    for (let i = nextLayout; i < studio.panes.length; i += 1) {
      const p = studio.panes[i];
      if (p.ro) {
        p.ro.disconnect();
        p.ro = null;
      }
      if (p.chart) {
        p.chart.remove();
        p.chart = null;
        p.mainSeries = null;
        p.overlaySeries = {};
        p.subPanes = {};
      }
      if (p.el && p.el.parentNode) p.el.remove();
    }

    // Mount active panes
    for (let i = 0; i < nextLayout; i += 1) {
      const p = studio.panes[i];
      if (!p.el) buildPaneDom(p);
      if (stage && !p.el.parentNode) stage.appendChild(p.el);
      p.el.classList.toggle("is-active", i === studio.activePaneIdx);
      ensurePaneChart(p);
      if (!p.data || !p.data.candles?.length) loadPaneChart(p, true);
      else renderPaneData(p, false);
    }

    const label = $("tv-layout-label");
    if (label) label.textContent = nextLayout === 1 ? "1 Pane" : nextLayout === 2 ? "2 Panes" : "4 Panes";
    for (const btn of document.querySelectorAll("[data-layout-opt]")) {
      btn.setAttribute("aria-pressed", String(Number(btn.dataset.layoutOpt) === nextLayout));
    }
    persistSettings();
  }

  function setActivePane(idx) {
    studio.activePaneIdx = idx;
    for (const p of studio.panes) {
      if (p.el) p.el.classList.toggle("is-active", p.idx === idx);
    }
    syncTopToolbarWithActivePane();
    const active = studio.panes[idx];
    if (active?.displayCandles?.length) {
      const botInfo = buildBotOverlayBoxes(active, active.displayCandles);
      renderBotPositionCards(botInfo.cards);
    }
    renderWatchlistRows();
  }

  function syncTopToolbarWithActivePane() {
    const pane = studio.panes[studio.activePaneIdx];
    if (!pane) return;
    const wlItem = studio.watchlistRows.find((r) => r.symbol === pane.symbol);
    const badgeEl = $("tv-active-badge");
    const symEl = $("tv-active-symbol");
    if (symEl) symEl.textContent = pane.symbol;
    if (badgeEl) {
      badgeEl.textContent = wlItem ? wlItem.badge : pane.symbol.slice(0, 3);
      badgeEl.style.background = wlItem ? wlItem.badge_bg : "#334155";
      badgeEl.style.color = wlItem ? wlItem.badge_fg : "#f8fafc";
    }
    for (const btn of document.querySelectorAll(".tv-tf-btn")) {
      btn.setAttribute("aria-pressed", String(btn.dataset.tf === pane.tf));
    }
    const alertSym = $("tv-alert-sym");
    if (alertSym) alertSym.textContent = pane.symbol;
  }

  function openSymbolInActivePane(rawSym, tf = null, fit = true) {
    const upper = String(rawSym || "NAS100").trim().toUpperCase();
    const sym = SYMBOL_ALIAS_TO_TV[upper] || upper;

    // Persist custom symbol if not in default list
    if (!studio.watchlistRows.some((r) => r.symbol === sym) && !studio.customSymbols.includes(sym)) {
      studio.customSymbols.push(sym);
      saveJsonStorage(STORAGE_KEY_CUSTOM_SYMS, studio.customSymbols);
      loadWatchlist(true);
    }

    const targets = studio.syncSymbol ? studio.panes.slice(0, studio.layout) : [studio.panes[studio.activePaneIdx]];
    for (const p of targets) {
      const changed = p.symbol !== sym || Boolean(tf && p.tf !== tf);
      p.symbol = sym;
      if (tf) p.tf = tf;
      if (changed) {
        p.data = null;
        p.rawCandles = [];
        p.displayCandles = [];
        if (p.symbol !== p._renderedSymbol) {
          p.mainSeries?.setData([]);
          for (const s of Object.values(p.overlaySeries || {})) s.setData([]);
          p.markersApi?.setMarkers([]);
          p.primitive?.setOverlays({ botBoxes: [], fvgs: [], sessions: [], pdhl: null });
        }
        resetPanePriceScale(p);
        if (p.legendMainEl) p.legendMainEl.textContent = `${sym} · ${p.tf.toUpperCase()} · Loading...`;
      }
      loadPaneChart(p, fit || changed);
    }
    syncTopToolbarWithActivePane();
    renderWatchlistRows();
    persistSettings();
  }

  // ===== Watchlist & Price Alerts =====
  function checkPriceAlerts(rows) {
    if (!studio.alerts.length) return;
    const bySym = new Map(rows.map((r) => [r.symbol, r]));
    let changed = false;
    for (const a of studio.alerts) {
      if (a.triggered) continue;
      const q = bySym.get(a.symbol);
      if (!q || !Number.isFinite(Number(q.price))) continue;
      const p = Number(q.price);
      const hit = (a.cond === "above" && p >= a.price) || (a.cond === "below" && p <= a.price);
      if (hit) {
        a.triggered = true;
        changed = true;
        const msg = `Price Alert: ${a.symbol} crossed ${a.cond} ${a.price} (now ${p})`;
        announceStatus(msg);
        if (studio.onStatusAlert) studio.onStatusAlert(msg);
      }
    }
    if (changed) {
      saveJsonStorage(STORAGE_KEY_ALERTS, studio.alerts);
      renderAlertsList();
    }
  }

  function renderAlertsList() {
    const list = $("tv-alert-list");
    const countPill = $("tv-alert-count");
    const activeCount = studio.alerts.filter((a) => !a.triggered).length;
    if (countPill) {
      countPill.textContent = String(activeCount);
      countPill.hidden = activeCount === 0;
    }
    if (!list) return;
    list.textContent = "";
    if (!studio.alerts.length) {
      const p = document.createElement("p");
      p.className = "empty-note";
      p.textContent = "No alerts yet. Add a target price above.";
      list.append(p);
      return;
    }
    for (const a of studio.alerts) {
      const row = document.createElement("div");
      row.className = "tv-alert-row";
      const txt = document.createElement("span");
      txt.textContent = `${a.symbol} ${a.cond === "above" ? "≥" : "≤"} ${a.price}${a.triggered ? " (Triggered)" : ""}`;
      if (a.triggered) txt.className = "pos";
      const del = document.createElement("button");
      del.type = "button";
      del.className = "tv-mini-btn";
      del.textContent = "×";
      del.setAttribute("aria-label", `Remove alert ${a.symbol}`);
      del.addEventListener("click", () => {
        studio.alerts = studio.alerts.filter((x) => x.id !== a.id);
        saveJsonStorage(STORAGE_KEY_ALERTS, studio.alerts);
        renderAlertsList();
      });
      row.append(txt, del);
      list.append(row);
    }
  }

  function renderWatchlistRows() {
    const list = $("tv-wl-list");
    if (!list) return;
    const activeSym = studio.panes[studio.activePaneIdx]?.symbol;
    const q = studio.watchlistSearch.trim().toUpperCase();
    const filter = studio.watchlistFilter;

    const filtered = studio.watchlistRows.filter((r) => {
      if (q && !r.symbol.includes(q) && !r.display_name.toUpperCase().includes(q)) return false;
      if (filter === "FLAGGED") return Boolean(studio.flags[r.symbol] && studio.flags[r.symbol] !== "none");
      if (filter === "CRYPTO") return r.category === "CRYPTO";
      if (filter === "FOREX") return r.category === "FOREX";
      if (filter === "INDEX_COMM") return ["INDEX", "METALS", "ENERGY"].includes(r.category);
      return true;
    });

    list.textContent = "";
    for (const r of filtered) {
      const rowBtn = document.createElement("div");
      rowBtn.className = "tv-wl-row";
      rowBtn.setAttribute("role", "option");
      rowBtn.setAttribute("tabindex", "0");
      rowBtn.setAttribute("aria-selected", String(r.symbol === activeSym));
      rowBtn.title = `${r.display_name} (${r.source})`;

      const symCell = document.createElement("div");
      symCell.className = "tv-wl-symcell";

      const flagBtn = document.createElement("button");
      flagBtn.type = "button";
      flagBtn.className = "tv-flag-btn";
      const curFlag = studio.flags[r.symbol] || "none";
      flagBtn.dataset.flag = curFlag;
      flagBtn.textContent = curFlag === "none" ? "⚑" : "⚑";
      flagBtn.setAttribute("aria-label", `Flag ${r.symbol}`);
      flagBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const order = ["none", "red", "blue", "green"];
        const next = order[(order.indexOf(studio.flags[r.symbol] || "none") + 1) % order.length];
        studio.flags[r.symbol] = next;
        saveJsonStorage(STORAGE_KEY_FLAGS, studio.flags);
        renderWatchlistRows();
      });

      const badge = document.createElement("span");
      badge.className = "tv-badge";
      badge.textContent = r.badge;
      badge.style.background = r.badge_bg;
      badge.style.color = r.badge_fg;

      const name = document.createElement("span");
      name.className = "tv-wl-name";
      name.textContent = r.symbol;

      symCell.append(flagBtn, badge, name);

      const priceEl = document.createElement("span");
      priceEl.className = "tv-wl-num";
      const isUp = Number(r.change || 0) >= 0;
      priceEl.style.color = r.price == null ? "#93a3c0" : isUp ? "#22c55e" : "#f87171";
      renderPricePipette(priceEl, r.price, r.digits || 2);

      const chgEl = document.createElement("span");
      chgEl.className = `tv-wl-num ${isUp ? "pos" : "neg"}`;
      chgEl.textContent = r.change == null ? "—" : formatSigned(r.change, r.digits || 2);

      const pctEl = document.createElement("span");
      pctEl.className = `tv-wl-num ${isUp ? "pos" : "neg"}`;
      pctEl.textContent = r.change_pct == null ? "—" : formatSigned(r.change_pct, 2, "%");

      rowBtn.append(symCell, priceEl, chgEl, pctEl);
      const activate = () => openSymbolInActivePane(r.symbol, null, true);
      rowBtn.addEventListener("click", activate);
      rowBtn.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate();
        }
      });
      list.append(rowBtn);
    }

    renderQuickSymbolJumpList();
  }

  function renderQuickSymbolJumpList() {
    const list = $("tv-quick-sym-list");
    if (!list) return;
    const q = ($("tv-quick-sym-input")?.value || "").trim().toUpperCase();
    list.textContent = "";
    for (const r of studio.watchlistRows) {
      if (q && !r.symbol.includes(q) && !r.display_name.toUpperCase().includes(q)) continue;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tv-pop-opt";
      btn.textContent = `${r.symbol} — ${r.display_name}`;
      btn.addEventListener("click", () => {
        openSymbolInActivePane(r.symbol, null, true);
        $("tv-symbol-popover")?.hidePopover?.();
      });
      list.append(btn);
    }
  }

  async function loadWatchlist(force = false) {
    if (!studio.apiJsonFn) return;
    try {
      const extra = studio.customSymbols.join(",");
      const data = await studio.apiJsonFn(`/api/tv/watchlist?extra=${encodeURIComponent(extra)}&force=${force ? 1 : 0}`);
      studio.watchlistRows = data.symbols || [];
      const upd = $("tv-wl-updated");
      if (upd) upd.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      renderWatchlistRows();
      syncTopToolbarWithActivePane();
      checkPriceAlerts(studio.watchlistRows);
    } catch (_) {
      // Handled upstream
    }
  }

  // ===== Bar Replay Controls =====
  function stopReplayTimer() {
    if (studio.replay.timerId !== null) {
      window.clearInterval(studio.replay.timerId);
      studio.replay.timerId = null;
    }
    studio.replay.playing = false;
    const playBtn = $("tv-replay-play");
    if (playBtn) playBtn.textContent = "Play";
  }

  function applyReplaySlice(pane, fit = false) {
    renderPaneData(pane, fit);
    const status = $("tv-replay-status");
    if (status && pane.rawCandles.length) {
      const idx = studio.replay.cutoffIdx ?? pane.rawCandles.length - 1;
      status.textContent = `Bar ${idx + 1} / ${pane.rawCandles.length} (${pane.symbol} ${pane.tf.toUpperCase()})`;
    }
  }

  function stepReplay() {
    const pane = studio.panes[studio.activePaneIdx];
    if (!pane || !pane.rawCandles.length) return;
    if (studio.replay.cutoffIdx === null) studio.replay.cutoffIdx = Math.max(10, Math.floor(pane.rawCandles.length * 0.65));
    if (studio.replay.cutoffIdx < pane.rawCandles.length - 1) {
      studio.replay.cutoffIdx += 1;
      applyReplaySlice(pane, false);
    } else {
      stopReplayTimer();
      announceStatus("Replay reached the latest candle.");
    }
  }

  // ===== Toolbar Fallback & Wiring =====
  function installRovingToolbarFallback(bar) {
    if (!bar || "focusGroup" in HTMLElement.prototype) return;
    const items = [...bar.querySelectorAll("button")];
    if (!items.length) return;
    items.forEach((b, i) => {
      b.tabIndex = i === 0 ? 0 : -1;
    });
    bar.addEventListener("focusin", ({ target }) => {
      if (items.includes(target)) items.forEach((b) => (b.tabIndex = b === target ? 0 : -1));
    });
    bar.addEventListener("keydown", (e) => {
      const cur = items.indexOf(document.activeElement);
      if (cur === -1) return;
      const vertical = bar.getAttribute("aria-orientation") === "vertical";
      const rtl = getComputedStyle(bar).direction === "rtl";
      const steps = vertical ? { ArrowDown: 1, ArrowUp: -1 } : { ArrowRight: rtl ? -1 : 1, ArrowLeft: rtl ? 1 : -1 };
      let next = -1;
      if (e.key in steps) next = (cur + steps[e.key] + items.length) % items.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = items.length - 1;
      if (next >= 0) {
        e.preventDefault();
        items[next].focus();
      }
    });
  }

  function syncScaleAndIndicatorUI() {
    const indCount = Object.values(studio.indicators).filter(Boolean).length;
    const badge = $("tv-ind-count");
    if (badge) badge.textContent = String(indCount);
    for (const cb of document.querySelectorAll("[data-ind]")) {
      cb.checked = Boolean(studio.indicators[cb.dataset.ind]);
    }
    for (const opt of document.querySelectorAll("[data-chart-style]")) {
      opt.setAttribute("aria-pressed", String(opt.dataset.chartStyle === studio.chartStyle));
    }
    const styleNames = { candles: "Candles", hollow: "Hollow", heikin: "Heikin-Ashi", bars: "Bars", line: "Line", area: "Area" };
    const sl = $("tv-style-label");
    if (sl) sl.textContent = styleNames[studio.chartStyle] || "Candles";

    $("tv-bot-boxes-btn")?.setAttribute("aria-pressed", String(studio.showBotBoxes));
    $("tv-magnet-btn")?.setAttribute("aria-pressed", String(studio.magnet));
    $("tv-lock-btn")?.setAttribute("aria-pressed", String(studio.locked));
    $("tv-scale-pct")?.setAttribute("aria-pressed", String(studio.scalePct));
    $("tv-scale-log")?.setAttribute("aria-pressed", String(studio.scaleLog));
    $("tv-scale-inv")?.setAttribute("aria-pressed", String(studio.scaleInv));
    $("tv-scale-auto")?.setAttribute("aria-pressed", String(studio.scaleAuto));
    const tzSel = $("tv-tz-select");
    if (tzSel) tzSel.value = studio.timezone;
    const syncCross = $("tv-sync-crosshair");
    if (syncCross) syncCross.checked = studio.syncCrosshair;
    const syncSym = $("tv-sync-symbol");
    if (syncSym) syncSym.checked = studio.syncSymbol;
  }

  function applyScaleOptionsToAll() {
    const LWC = window.LightweightCharts;
    if (!LWC) return;
    const mode = studio.scalePct
      ? LWC.PriceScaleMode.Percentage
      : studio.scaleLog
        ? LWC.PriceScaleMode.Logarithmic
        : LWC.PriceScaleMode.Normal;
    for (const p of studio.panes.slice(0, studio.layout)) {
      if (!p.chart) continue;
      p.chart.applyOptions({
        rightPriceScale: { mode, invertScale: studio.scaleInv, autoScale: studio.scaleAuto },
        timeScale: { tickMarkFormatter: (t) => getAxisFormatter().format(new Date(Number(t) * 1000)) },
      });
      p.mainSeries?.priceScale().setAutoScale(studio.scaleAuto);
    }
  }

  function wireEvents() {
    installRovingToolbarFallback(document.querySelector(".tv-topbar"));
    installRovingToolbarFallback($("tv-drawbar"));

    // Timeframes
    for (const btn of document.querySelectorAll(".tv-tf-btn")) {
      btn.addEventListener("click", () => {
        const tf = btn.dataset.tf;
        const pane = studio.panes[studio.activePaneIdx];
        if (!pane) return;
        pane.tf = tf;
        syncTopToolbarWithActivePane();
        loadPaneChart(pane, true);
        persistSettings();
      });
    }

    // Chart style
    for (const btn of document.querySelectorAll("[data-chart-style]")) {
      btn.addEventListener("click", () => {
        studio.chartStyle = btn.dataset.chartStyle;
        syncScaleAndIndicatorUI();
        for (const p of studio.panes.slice(0, studio.layout)) {
          createMainSeriesForStyle(p);
          renderPaneData(p, false);
        }
        $("tv-style-popover")?.hidePopover?.();
        persistSettings();
      });
    }

    // Indicators
    for (const cb of document.querySelectorAll("[data-ind]")) {
      cb.addEventListener("change", () => {
        studio.indicators[cb.dataset.ind] = Boolean(cb.checked);
        syncScaleAndIndicatorUI();
        for (const p of studio.panes.slice(0, studio.layout)) renderPaneData(p, false);
        persistSettings();
      });
    }

    // Layout
    for (const btn of document.querySelectorAll("[data-layout-opt]")) {
      btn.addEventListener("click", () => {
        applyLayout(Number(btn.dataset.layoutOpt || 1));
        $("tv-layout-popover")?.hidePopover?.();
      });
    }
    $("tv-sync-crosshair")?.addEventListener("change", (e) => {
      studio.syncCrosshair = Boolean(e.target.checked);
      persistSettings();
    });
    $("tv-sync-symbol")?.addEventListener("change", (e) => {
      studio.syncSymbol = Boolean(e.target.checked);
      persistSettings();
    });

    // Bot boxes & RR
    $("tv-bot-boxes-btn")?.addEventListener("click", () => {
      studio.showBotBoxes = !studio.showBotBoxes;
      syncScaleAndIndicatorUI();
      for (const p of studio.panes.slice(0, studio.layout)) renderPaneData(p, false);
      persistSettings();
    });
    for (const btn of document.querySelectorAll(".chart-rr-btn")) {
      btn.addEventListener("click", () => {
        studio.targetR = Number(btn.dataset.rr || 3);
        for (const b of document.querySelectorAll(".chart-rr-btn")) {
          b.setAttribute("aria-pressed", String(Number(b.dataset.rr) === studio.targetR));
        }
        for (const p of studio.panes.slice(0, studio.layout)) renderPaneData(p, false);
      });
    }
    $("clear-inspected-trade-btn")?.addEventListener("click", () => {
      studio.inspectedTrade = null;
      const active = studio.panes[studio.activePaneIdx];
      if (active) renderPaneData(active, true);
    });

    // Drawing tool buttons
    for (const btn of document.querySelectorAll(".tv-tool-btn[data-tool]")) {
      btn.addEventListener("click", () => setActiveTool(btn.dataset.tool));
    }
    $("tv-magnet-btn")?.addEventListener("click", () => {
      studio.magnet = !studio.magnet;
      syncScaleAndIndicatorUI();
      persistSettings();
    });
    $("tv-lock-btn")?.addEventListener("click", () => {
      studio.locked = !studio.locked;
      syncScaleAndIndicatorUI();
      persistSettings();
    });
    $("tv-undo-btn")?.addEventListener("click", undoDrawing);
    $("tv-delete-btn")?.addEventListener("click", deleteSelectedDrawing);
    $("tv-clear-draw-btn")?.addEventListener("click", clearSymbolDrawings);

    // Fit, Snapshot, Fullscreen, Watchlist collapse
    $("tv-fit-btn")?.addEventListener("click", () => {
      const p = studio.panes[studio.activePaneIdx];
      if (!p || !p.chart || !p.displayCandles.length) return;
      const n = p.displayCandles.length;
      p.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 120), to: n + 10 });
      resetPanePriceScale(p);
    });
    $("tv-shot-btn")?.addEventListener("click", () => {
      const p = studio.panes[studio.activePaneIdx];
      if (!p?.chart) return;
      const canvas = p.chart.takeScreenshot();
      const link = document.createElement("a");
      link.download = `${p.symbol}_${p.tf}_chart.png`;
      link.href = canvas.toDataURL("image/png");
      link.click();
      announceStatus(`Saved ${p.symbol} ${p.tf.toUpperCase()} PNG snapshot.`);
    });
    $("tv-fullscreen-btn")?.addEventListener("click", async () => {
      const box = $("tv-studio");
      if (!box) return;
      try {
        if (!document.fullscreenElement) {
          await box.requestFullscreen();
          $("tv-fullscreen-btn").setAttribute("aria-pressed", "true");
        } else {
          await document.exitFullscreen();
          $("tv-fullscreen-btn").setAttribute("aria-pressed", "false");
        }
      } catch (_) {}
    });
    $("tv-watchlist-toggle-btn")?.addEventListener("click", () => {
      studio.watchlistCollapsed = !studio.watchlistCollapsed;
      $("tv-studio")?.classList.toggle("watchlist-collapsed", studio.watchlistCollapsed);
      const wl = $("tv-watchlist");
      if (wl) wl.hidden = studio.watchlistCollapsed;
      $("tv-watchlist-toggle-btn")?.setAttribute("aria-pressed", String(!studio.watchlistCollapsed));
    });

    // Bottom bar range & scales & timezone
    for (const btn of document.querySelectorAll(".tv-range-btn")) {
      btn.addEventListener("click", () => {
        const p = studio.panes[studio.activePaneIdx];
        if (!p?.chart || !p.displayCandles.length) return;
        const n = p.displayCandles.length;
        const r = btn.dataset.range;
        const from = r === "all" ? 0 : Math.max(0, n - Number(r));
        p.chart.timeScale().setVisibleLogicalRange({ from, to: n + 8 });
        resetPanePriceScale(p);
      });
    }
    $("tv-scale-pct")?.addEventListener("click", () => {
      studio.scalePct = !studio.scalePct;
      if (studio.scalePct) studio.scaleLog = false;
      syncScaleAndIndicatorUI();
      applyScaleOptionsToAll();
    });
    $("tv-scale-log")?.addEventListener("click", () => {
      studio.scaleLog = !studio.scaleLog;
      if (studio.scaleLog) studio.scalePct = false;
      syncScaleAndIndicatorUI();
      applyScaleOptionsToAll();
    });
    $("tv-scale-inv")?.addEventListener("click", () => {
      studio.scaleInv = !studio.scaleInv;
      syncScaleAndIndicatorUI();
      applyScaleOptionsToAll();
    });
    $("tv-scale-auto")?.addEventListener("click", () => {
      const p = studio.panes[studio.activePaneIdx];
      let actual = studio.scaleAuto;
      try {
        const liveOpt = p?.mainSeries?.priceScale()?.options()?.autoScale;
        if (typeof liveOpt === "boolean") actual = liveOpt;
      } catch (_) {}
      studio.scaleAuto = !actual;
      syncScaleAndIndicatorUI();
      applyScaleOptionsToAll();
    });
    $("tv-tz-select")?.addEventListener("change", (e) => {
      studio.timezone = e.target.value;
      applyScaleOptionsToAll();
      persistSettings();
    });

    // Replay bar
    $("tv-replay-btn")?.addEventListener("click", () => {
      const p = studio.panes[studio.activePaneIdx];
      studio.replay.active = !studio.replay.active;
      $("tv-replay-btn")?.setAttribute("aria-pressed", String(studio.replay.active));
      const bar = $("tv-replay-bar");
      if (bar) bar.hidden = !studio.replay.active;
      if (studio.replay.active && p?.rawCandles?.length) {
        studio.replay.picking = true;
        studio.replay.cutoffIdx = Math.max(20, Math.floor(p.rawCandles.length * 0.7));
        applyReplaySlice(p, true);
        announceStatus("Replay started — click any candle to jump or press Step +1 / Play.");
      } else {
        stopReplayTimer();
        studio.replay.picking = false;
        studio.replay.cutoffIdx = null;
        if (p) renderPaneData(p, true);
      }
    });
    $("tv-replay-pick")?.addEventListener("click", () => {
      studio.replay.picking = true;
      announceStatus("Click any candle on the chart to set replay start.");
    });
    $("tv-replay-step")?.addEventListener("click", stepReplay);
    $("tv-replay-play")?.addEventListener("click", () => {
      if (studio.replay.playing) {
        stopReplayTimer();
      } else {
        studio.replay.playing = true;
        $("tv-replay-play").textContent = "Pause";
        studio.replay.timerId = window.setInterval(stepReplay, studio.replay.speedMs);
      }
    });
    $("tv-replay-speed")?.addEventListener("change", (e) => {
      studio.replay.speedMs = Number(e.target.value || 800);
      if (studio.replay.playing) {
        stopReplayTimer();
        studio.replay.playing = true;
        $("tv-replay-play").textContent = "Pause";
        studio.replay.timerId = window.setInterval(stepReplay, studio.replay.speedMs);
      }
    });
    $("tv-replay-exit")?.addEventListener("click", () => {
      if (studio.replay.active) $("tv-replay-btn")?.click();
    });

    // Watchlist search/add + filters
    $("tv-wl-search-input")?.addEventListener("input", (e) => {
      studio.watchlistSearch = e.target.value;
      renderWatchlistRows();
    });
    $("tv-wl-add-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const raw = ($("tv-wl-search-input")?.value || "").trim().toUpperCase();
      if (!raw) return;
      openSymbolInActivePane(raw, null, true);
      $("tv-wl-search-input").value = "";
      studio.watchlistSearch = "";
    });
    for (const btn of document.querySelectorAll(".tv-chip-btn[data-wl-filter]")) {
      btn.addEventListener("click", () => {
        studio.watchlistFilter = btn.dataset.wlFilter;
        for (const b of document.querySelectorAll(".tv-chip-btn[data-wl-filter]")) {
          b.setAttribute("aria-pressed", String(b === btn));
        }
        renderWatchlistRows();
      });
    }

    // Quick Symbol Jump Popover
    $("tv-quick-sym-input")?.addEventListener("input", renderQuickSymbolJumpList);
    $("tv-quick-sym-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const val = ($("tv-quick-sym-input")?.value || "").trim();
      if (!val) return;
      openSymbolInActivePane(val, null, true);
      $("tv-symbol-popover")?.hidePopover?.();
    });

    // Price Alerts Form
    $("tv-alert-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const pane = studio.panes[studio.activePaneIdx];
      const price = Number($("tv-alert-price")?.value);
      const cond = $("tv-alert-cond")?.value || "above";
      if (!pane || !Number.isFinite(price)) return;
      studio.alerts.push({
        id: `al_${Date.now()}`,
        symbol: pane.symbol,
        cond,
        price,
        triggered: false,
      });
      saveJsonStorage(STORAGE_KEY_ALERTS, studio.alerts);
      $("tv-alert-price").value = "";
      renderAlertsList();
      announceStatus(`Added alert: ${pane.symbol} ${cond} ${price}`);
    });

    // Global TradingView Keyboard Shortcuts (only when Chart tab is active and not typing in an input)
    document.addEventListener("keydown", (e) => {
      if (!studio.visible) return;
      const tag = (e.target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (e.key === "Escape") {
        setActiveTool("cursor");
        studio.selectedDrawing = null;
        redrawAllPanes();
      } else if (e.key === "Delete" || e.key === "Backspace") {
        if (studio.selectedDrawing) {
          e.preventDefault();
          deleteSelectedDrawing();
        }
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        undoDrawing();
      } else if (e.key === "/") {
        e.preventDefault();
        $("tv-symbol-popover")?.showPopover?.();
        $("tv-quick-sym-input")?.focus();
      } else if (e.key.toLowerCase() === "m" && !e.altKey) {
        studio.magnet = !studio.magnet;
        syncScaleAndIndicatorUI();
        persistSettings();
      } else if (e.altKey) {
        const k = e.key.toLowerCase();
        if (k === "t") setActiveTool("trendline");
        else if (k === "h") setActiveTool("hline");
        else if (k === "r") setActiveTool("rect");
        else if (k === "f") setActiveTool("fib");
        else if (k === "m") setActiveTool("measure");
      }
    });
  }

  function startWatchlistPolling() {
    if (studio.wlTimerId !== null) return;
    studio.wlTimerId = window.setInterval(() => {
      if (document.hidden || !studio.visible) return;
      loadWatchlist(false);
      if (!studio.replay.active) {
        for (const p of studio.panes.slice(0, studio.layout)) {
          loadPaneChart(p, false);
        }
      }
    }, 6000);
  }

  window.TVStudio = {
    init({ apiJson, onStatusAlert }) {
      if (studio.initialized) return;
      studio.initialized = true;
      studio.apiJsonFn = apiJson;
      studio.onStatusAlert = onStatusAlert;
      loadPersistedState();
      wireEvents();
      syncScaleAndIndicatorUI();
      syncTopToolbarWithActivePane();
      renderAlertsList();
      startWatchlistPolling();
    },

    onTabVisibility(isChartTab) {
      studio.visible = isChartTab;
      document.querySelector(".app-shell")?.classList.toggle("chart-wide", isChartTab);
      if (isChartTab) {
        applyLayout(studio.layout);
        loadWatchlist(false);
      }
    },

    setSnapshot(snap) {
      studio.snapshotRef = snap;
      if (studio.visible) {
        for (const p of studio.panes.slice(0, studio.layout)) {
          if (p.data) renderPaneData(p, false);
        }
      }
    },

    openSymbol(symbol, tf = null, inspectedTrade = null) {
      studio.inspectedTrade = inspectedTrade;
      openSymbolInActivePane(symbol, tf, !inspectedTrade);
    },
  };
})();
