"use strict";

(() => {
  const REFRESH_INTERVAL_SEC = 10;
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

  const state = {
    scope: "all",
    activeTab: "overview",
    snapshot: null,
    reports: [],
    logs: [],
    countdown: REFRESH_INTERVAL_SEC,
    timerId: null,
    equityPoints: [],
  };

  const nyAxisFmt = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  // DOM helpers
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs = {}, text = "") {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null) continue;
      if (k === "class") {
        node.className = v;
      } else if (k === "dataset") {
        for (const [dk, dv] of Object.entries(v)) {
          node.dataset[dk] = String(dv);
        }
      } else {
        node.setAttribute(k, String(v));
      }
    }
    if (text !== "") node.textContent = text;
    return node;
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function fmtMoney(val, showSign = false) {
    const num = Number(val || 0);
    const abs = Math.abs(num).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    if (num < 0) return `-$${abs}`;
    if (showSign && num > 0) return `+$${abs}`;
    return `$${abs}`;
  }

  function fmtNum(val, digits = 2) {
    const num = Number(val);
    if (!Number.isFinite(num)) return "—";
    return num.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function isNetworkFetchError(err) {
    return Boolean(err && (err.name === "TypeError" || /failed to fetch|networkerror|load failed/i.test(String(err.message || ""))));
  }

  // Auth flow
  function showAuthView(errorMsg = "", markFieldInvalid = false) {
    stopPolling();
    $("app-view").hidden = true;
    $("auth-view").hidden = false;
    const errEl = $("login-error");
    const input = $("passcode-input");
    if (errorMsg) {
      errEl.textContent = errorMsg;
      errEl.hidden = false;
      if (markFieldInvalid) {
        input.setAttribute("aria-invalid", "true");
      } else {
        input.removeAttribute("aria-invalid");
      }
    } else {
      errEl.textContent = "";
      errEl.hidden = true;
      input.removeAttribute("aria-invalid");
    }
    input.value = "";
    input.focus();
  }

  function applyRequestedInitialTab() {
    if (state.initialTabApplied) return;
    state.initialTabApplied = true;
    const reqTab = (new URLSearchParams(window.location.search).get("tab") || "").toLowerCase();
    const targetBtn = reqTab ? document.getElementById(`tab-${reqTab}`) : null;
    if (targetBtn && targetBtn.getAttribute("role") === "tab") {
      targetBtn.click();
    }
  }

  function showAppView() {
    $("auth-view").hidden = true;
    $("app-view").hidden = false;
    applyRequestedInitialTab();
    startPolling();
    refreshAll(true);
  }

  async function apiJson(url, options = {}) {
    const resp = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (resp.status === 401 && url !== "/api/login" && url !== "/api/auth") {
      showAuthView("Session locked. Please enter your passcode again.");
      throw new Error("Unauthorized");
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    return data;
  }

  function setupAuthForm() {
    const form = $("login-form");
    const input = $("passcode-input");
    const errEl = $("login-error");
    const submitBtn = $("login-submit");
    const toggleBtn = $("toggle-passcode");

    toggleBtn.addEventListener("click", () => {
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      toggleBtn.setAttribute("aria-pressed", String(!showing));
      toggleBtn.setAttribute("aria-label", showing ? "Show passcode" : "Hide passcode");
      toggleBtn.textContent = showing ? "Show" : "Hide";
    });

    input.addEventListener("input", () => {
      input.setCustomValidity("");
      input.removeAttribute("aria-invalid");
      errEl.hidden = true;
      errEl.textContent = "";
    });

    input.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if (event.key === "Enter") {
        form.requestSubmit();
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const val = input.value.trim();
      if (val.length < 4) {
        const msg = "Please enter the passcode (at least 4 characters).";
        input.setCustomValidity(msg);
        input.setAttribute("aria-invalid", "true");
        errEl.textContent = msg;
        errEl.hidden = false;
        input.focus();
        return;
      }

      submitBtn.disabled = true;
      submitBtn.textContent = "Unlocking...";
      try {
        await apiJson("/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ passcode: val }),
        });
        showAppView();
      } catch (err) {
        if (isNetworkFetchError(err)) {
          errEl.textContent = "Cannot reach TradeTerminal Server. Start Launch_TradeTerminal.bat (or terminal_server.py) first, then try again.";
          errEl.hidden = false;
          input.removeAttribute("aria-invalid");
        } else {
          errEl.textContent = err.message || "Invalid passcode";
          errEl.hidden = false;
          input.setAttribute("aria-invalid", "true");
          input.focus();
          input.select();
        }
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Open Terminal";
      }
    });

    $("logout-btn").addEventListener("click", async () => {
      try {
        await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
      } catch (_) {
        // Ignore network errors on lock
      }
      showAuthView();
    });
  }

  // Tabs (accessible ARIA tablist + roving focus + responsive horizontal scroll)
  function setupTabs() {
    const tablist = document.querySelector('[role="tablist"]');
    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    const panels = new Map(
      tabs.map((t) => [t, document.querySelector(`[role="tabpanel"][aria-labelledby="${t.id}"]`)])
    );

    function activateTab(tab, moveFocus = false) {
      state.activeTab = tab.id.replace("tab-", "");
      for (const t of tabs) {
        const selected = t === tab;
        t.setAttribute("aria-selected", String(selected));
        t.setAttribute("tabindex", selected ? "0" : "-1");
        const panel = panels.get(t);
        if (panel) panel.hidden = !selected;
      }
      tab.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (moveFocus) tab.focus();

      window.TVStudio?.onTabVisibility(state.activeTab === "chart");
      if (state.activeTab === "overview" && state.snapshot) {
        requestAnimationFrame(() => drawEquityChart(state.snapshot.performance.equity_curve, state.snapshot.performance.initial_balance));
      } else if (state.activeTab === "radar" && state.snapshot) {
        requestAnimationFrame(() => renderRadarCharts(state.snapshot.radar));
      } else if (state.activeTab === "reports" && !state.reports.length) {
        loadReports();
      } else if (state.activeTab === "logs") {
        loadLogs();
      }
    }

    tablist.addEventListener("click", (event) => {
      const btn = event.target.closest('[role="tab"]');
      if (btn) activateTab(btn, false);
    });

    tablist.addEventListener("keydown", (event) => {
      const current = event.target.closest('[role="tab"]');
      if (!current) return;
      const idx = tabs.indexOf(current);
      let nextIdx = -1;
      if (event.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
      else if (event.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") nextIdx = 0;
      else if (event.key === "End") nextIdx = tabs.length - 1;
      if (nextIdx >= 0) {
        event.preventDefault();
        activateTab(tabs[nextIdx], true);
      }
    });
  }

  // Sortable tables (preserving <tbody> row identity via moveBefore / appendChild)
  function setupSortableTable(tableId, statusId) {
    const table = $(tableId);
    const tbody = table.tBodies[0];
    const status = $(statusId);

    table.tHead.addEventListener("click", (event) => {
      const btn = event.target.closest("button.sort-btn");
      if (!btn) return;
      const th = btn.closest("th");
      const direction = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";

      for (const header of table.tHead.rows[0].cells) {
        header.removeAttribute("aria-sort");
      }
      th.setAttribute("aria-sort", direction);

      const colIdx = th.cellIndex;
      const type = th.dataset.type || "text";
      const factor = direction === "ascending" ? 1 : -1;
      const rows = [...tbody.rows];

      const keyed = rows.map((row) => {
        const cell = row.cells[colIdx];
        const raw = cell?.dataset.value ?? cell?.textContent.trim() ?? "";
        return { row, raw, num: type === "number" ? Number(raw) : 0 };
      });

      keyed.sort((a, b) => {
        if (type === "number") return factor * (a.num - b.num);
        return factor * collator.compare(a.raw, b.raw);
      });

      const moveNode = typeof tbody.moveBefore === "function"
        ? (r) => tbody.moveBefore(r, null)
        : (r) => tbody.appendChild(r);
      for (const item of keyed) moveNode(item.row);

      status.textContent = `Sorted by ${btn.textContent.trim()}, ${direction}, ${rows.length} rows`;
    });
  }

  // Data loading & polling
  async function refreshAll(force = false) {
    try {
      const snap = await apiJson(`/api/snapshot?scope=${encodeURIComponent(state.scope)}&force=${force ? 1 : 0}`);
      state.snapshot = snap;
      state.countdown = REFRESH_INTERVAL_SEC;
      renderSnapshot(snap);
      window.TVStudio?.setSnapshot(snap);
      if (state.activeTab === "logs") await loadLogs();
      if (state.activeTab === "reports" && !state.reports.length) await loadReports();
    } catch (err) {
      if (err.message !== "Unauthorized") {
        showAlert(`Live refresh warning: ${err.message}`);
      }
    }
  }

  async function loadReports() {
    try {
      const data = await apiJson("/api/reports");
      state.reports = data.strategies || [];
      renderReports(state.reports, state.snapshot);
    } catch (_) {
      // Handled upstream
    }
  }

  async function loadLogs() {
    try {
      const data = await apiJson("/api/logs?limit=150");
      state.logs = data.lines || [];
      renderLogs(state.logs, state.snapshot?.bot);
    } catch (_) {
      // Handled upstream
    }
  }

  function startPolling() {
    stopPolling();
    state.countdown = REFRESH_INTERVAL_SEC;
    $("countdown-label").textContent = `(${state.countdown}s)`;
    state.timerId = window.setInterval(() => {
      if (document.hidden) return;
      state.countdown -= 1;
      if (state.countdown <= 0) {
        refreshAll(false);
        state.countdown = REFRESH_INTERVAL_SEC;
      }
      $("countdown-label").textContent = `(${state.countdown}s)`;
    }, 1000);
  }

  function stopPolling() {
    if (state.timerId !== null) {
      window.clearInterval(state.timerId);
      state.timerId = null;
    }
  }

  function showAlert(msg) {
    const banner = $("alert-banner");
    if (!msg) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    banner.textContent = msg;
    banner.hidden = false;
  }

  // Renderers
  function renderSnapshot(snap) {
    const acct = snap.account || {};
    const bot = snap.bot || {};
    const perf = snap.performance || {};

    // Topbar
    const dot = $("mt5-status-dot");
    dot.className = `status-dot ${snap.mt5_connected ? "online" : "offline"}`;

    const modeBadge = $("acct-mode-badge");
    modeBadge.textContent = acct.mode || "DEMO";
    modeBadge.className = `pill ${acct.mode === "REAL" ? "red" : "accent"}`;

    const botBadge = $("bot-alive-badge");
    if (bot.alive) {
      botBadge.textContent = bot.dry_run ? "BOT DRY-RUN" : "BOT RUNNING";
      botBadge.className = `pill ${bot.dry_run ? "amber" : "green"}`;
    } else if (bot.present) {
      botBadge.textContent = "BOT STOPPED";
      botBadge.className = "pill amber";
    } else {
      botBadge.textContent = "BOT NOT STARTED";
      botBadge.className = "pill neutral";
    }

    $("acct-meta").textContent = `MT5 ${acct.login || "—"} (${acct.server || "Offline"}) · ${acct.leverage ? `1:${acct.leverage}` : "—"}`;
    $("ny-clock").textContent = `NY ${snap.generated_at_ny}`;
    $("window-badge").textContent = snap.in_window ? "Market Window OPEN" : "Market Window CLOSED";

    if (!snap.mt5_connected) {
      showAlert(snap.mt5_error || "MetaTrader 5 desktop app is offline. Showing last known snapshot.");
    } else if (acct.algo_allowed === false) {
      showAlert("MT5 Algo Trading button is currently OFF inside MetaTrader 5. Enable it before Monday live execution.");
    } else if (bot.dd_blocked) {
      showAlert(`Daily DD guard triggered (${fmtNum(bot.daily_dd_pct, 2)}% >= ${fmtNum(bot.max_daily_dd_pct, 1)}%); new bot entries paused today.`);
    } else {
      showAlert("");
    }

    // KPI Cards
    $("kpi-balance").textContent = fmtMoney(acct.balance);
    $("kpi-acct-login").textContent = `Init ${fmtMoney(perf.initial_balance)} · ${acct.currency || "USD"}`;

    const flt = Number(acct.floating_pnl || 0);
    $("kpi-equity").textContent = fmtMoney(acct.equity);
    const fltEl = $("kpi-floating");
    fltEl.textContent = `Floating ${fmtMoney(flt, true)}`;
    fltEl.className = `kpi-sub ${flt >= 0 ? "pos" : "neg"}`;

    const netEl = $("kpi-net-pnl");
    netEl.textContent = fmtMoney(perf.net_pnl, true);
    netEl.className = `kpi-value ${Number(perf.net_pnl || 0) >= 0 ? "pos" : "neg"}`;
    $("kpi-return-pct").textContent = `Return ${Number(perf.return_pct || 0) >= 0 ? "+" : ""}${fmtNum(perf.return_pct, 2)}% (${snap.scope === "bot" ? "Bot" : "Account"})`;

    $("kpi-win-pf").textContent = `${fmtNum(perf.win_rate_pct, 1)}% · PF ${fmtNum(perf.profit_factor, 2)}`;
    $("kpi-wl-count").textContent = `${perf.total_trades || 0} closed (${perf.wins || 0}W / ${perf.losses || 0}L)`;

    $("kpi-max-dd").textContent = `${fmtNum(perf.max_drawdown_pct, 2)}%`;
    $("kpi-expectancy").textContent = `DD ${fmtMoney(perf.max_drawdown_usd)} · Avg ${fmtMoney(perf.expectancy, true)}`;

    const ddVal = Number(bot.daily_dd_pct || 0);
    $("kpi-daily-dd").textContent = `${fmtNum(ddVal, 2)}% / ${fmtNum(bot.max_daily_dd_pct || 3.0, 1)}%`;
    $("kpi-strategy").textContent = `${bot.strategy || "DONCHIAN_1H"} · ${bot.risk_pct || 1}% risk`;

    // Equity Chart
    const curve = perf.equity_curve || [];
    $("equity-range-pill").textContent = `${curve.length > 1 ? curve.length - 1 : 0} pts`;
    if (state.activeTab === "overview") {
      drawEquityChart(curve, perf.initial_balance);
    }

    // By-Symbol Summary
    renderSymbolBreakdown(perf.by_symbol || []);

    // Open Positions
    renderPositions(snap.positions || []);

    // Closed Trades Table
    renderTradesTable(snap.trades || []);

    // Radar Cards & Reports Benchmark
    renderRadar(snap.radar || []);
    if (state.reports.length) {
      renderReports(state.reports, snap);
    }
  }

  function renderSymbolBreakdown(items) {
    const container = $("symbol-breakdown-list");
    clearChildren(container);
    if (!items.length) {
      container.appendChild(el("p", { class: "empty-note" }, "No closed trades per symbol yet."));
      return;
    }
    for (const item of items) {
      const left = el("div", {});
      left.appendChild(el("strong", {}, item.symbol));
      left.appendChild(
        el("div", { class: "kpi-sub" }, `${item.trades} trades · ${fmtNum(item.win_rate_pct, 1)}% WR · PF ${fmtNum(item.profit_factor, 2)}`)
      );
      const pnlClass = Number(item.net_pnl) >= 0 ? "pos" : "neg";
      const right = el("strong", { class: pnlClass }, fmtMoney(item.net_pnl, true));
      const row = el("div", { class: "symbol-stat-row" });
      row.append(left, right);
      container.appendChild(row);
    }
  }

  function renderPositions(positions) {
    const container = $("open-positions-container");
    clearChildren(container);
    $("open-pos-count").textContent = `${positions.length} open`;
    if (!positions.length) {
      container.appendChild(el("p", { class: "empty-note" }, "No open MT5 positions right now. Account is 100% flat."));
      return;
    }
    for (const p of positions) {
      const card = el("div", { class: "pos-card" });
      const head = el("div", { class: "pos-card-head" });
      const titleGroup = el("div", { class: "brand-title-row" });
      titleGroup.appendChild(el("strong", {}, `${p.symbol} ${p.direction}`));
      titleGroup.appendChild(el("span", { class: `pill ${p.is_bot ? "accent" : "neutral"}` }, p.is_bot ? "BOT" : "MANUAL"));

      const rightHead = el("div", { class: "brand-title-row" });
      const pnl = Number(p.profit || 0);
      rightHead.appendChild(el("strong", { class: pnl >= 0 ? "pos" : "neg" }, fmtMoney(pnl, true)));
      const chartBtn = el("button", { type: "button", class: "btn-mini" }, "Chart");
      chartBtn.addEventListener("click", () => openPositionOnChart(p));
      rightHead.appendChild(chartBtn);
      head.append(titleGroup, rightHead);

      const grid = el("div", { class: "pos-metrics" });
      const d = p.digits || 2;
      grid.append(
        el("div", {}, `Lots: ${fmtNum(p.volume, 2)}`),
        el("div", {}, `Ticket: #${p.ticket}`),
        el("div", {}, `Entry: ${fmtNum(p.price_open, d)}`),
        el("div", {}, `Current: ${fmtNum(p.price_current, d)}`),
        el("div", {}, `Stop Loss: ${p.sl > 0 ? fmtNum(p.sl, d) : "None"}`),
        el("div", {}, `Opened: ${p.time_ny}`)
      );

      card.append(head, grid);
      container.appendChild(card);
    }
  }

  function renderTradesTable(trades) {
    const filterSel = $("trade-symbol-filter");
    const prevFilter = filterSel.value || "ALL";
    const uniqueSyms = [...new Set(trades.map((t) => t.symbol).filter(Boolean))].sort();

    clearChildren(filterSel);
    filterSel.appendChild(el("option", { value: "ALL" }, "All Symbols"));
    for (const s of uniqueSyms) {
      filterSel.appendChild(el("option", { value: s }, s));
    }
    filterSel.value = uniqueSyms.includes(prevFilter) ? prevFilter : "ALL";

    const chosen = filterSel.value;
    const filtered = chosen === "ALL" ? trades : trades.filter((t) => t.symbol === chosen);

    const tbody = $("trades-tbody");
    clearChildren(tbody);
    $("trades-empty").hidden = filtered.length > 0;
    $("trades-sub").textContent = `${filtered.length} closed position(s) (${state.scope === "bot" ? "Bot magic filter active" : "All deals in account"})`;

    for (const t of filtered) {
      const tr = el("tr");
      tr.appendChild(el("th", { scope: "row", dataset: { value: t.exit_epoch } }, t.exit_time_ny));
      tr.appendChild(el("td", { dataset: { value: t.symbol } }, t.symbol));
      tr.appendChild(el("td", { dataset: { value: t.direction } }, t.direction));
      tr.appendChild(el("td", { dataset: { value: t.volume } }, fmtNum(t.volume, 2)));
      tr.appendChild(el("td", { dataset: { value: t.entry_price } }, fmtNum(t.entry_price, 2)));
      tr.appendChild(el("td", { dataset: { value: t.exit_price } }, fmtNum(t.exit_price, 2)));
      tr.appendChild(el("td", { dataset: { value: t.duration_hours } }, `${fmtNum(t.duration_hours, 1)}h`));

      const pnlTd = el("td", {
        class: Number(t.net_pnl) >= 0 ? "pos" : "neg",
        dataset: { value: t.net_pnl },
      }, fmtMoney(t.net_pnl, true));
      tr.appendChild(pnlTd);

      const srcTd = el("td");
      srcTd.appendChild(el("span", { class: `pill ${t.is_bot ? "accent" : "neutral"}` }, t.is_bot ? "BOT" : "MANUAL"));
      tr.appendChild(srcTd);

      const actTd = el("td");
      const viewBtn = el("button", { type: "button", class: "btn-mini" }, "Inspect");
      viewBtn.addEventListener("click", () => inspectClosedTradeOnChart(t));
      actTd.appendChild(viewBtn);
      tr.appendChild(actTd);

      tbody.appendChild(tr);
    }
  }

  function renderRadar(radarItems) {
    const grid = $("radar-grid");
    clearChildren(grid);
    if (!radarItems.length) {
      grid.appendChild(el("p", { class: "empty-note" }, "Connect MT5 desktop terminal to load live 1H Donchian radar for USTEC and XAUUSD."));
      return;
    }

    for (const item of radarItems) {
      const card = el("article", { class: "card" });
      const header = el("header", { class: "card-header" });
      const titleDiv = el("div", {});
      titleDiv.appendChild(el("h2", {}, `${item.symbol} (${item.canonical})`));
      titleDiv.appendChild(el("p", { class: "card-sub" }, item.last_bar_ny ? `Last closed 1H bar: ${item.last_bar_ny}` : `Status: ${item.status}`));

      const alignClass = item.alignment === "BULLISH" ? "green" : item.alignment === "BEARISH" ? "red" : "amber";
      header.append(titleDiv, el("span", { class: `pill ${alignClass}` }, `${item.alignment || "WAIT"} BIAS`));
      card.appendChild(header);

      if (item.status !== "READY") {
        card.appendChild(el("p", { class: "empty-note" }, `Waiting for sufficient 1H bars (loaded ${item.bars_count || 0}).`));
        grid.appendChild(card);
        continue;
      }

      const d = item.digits || 2;

      // Channel position gauge
      const gauge = el("div", { class: "radar-gauge" });
      const track = el("div", { class: "gauge-track" });
      const fill = el("div", { class: "gauge-fill" });
      fill.style.width = `${item.channel_pct}%`;
      track.appendChild(fill);
      const labels = el("div", { class: "gauge-labels" });
      labels.append(
        el("span", {}, `Short Trigger: ${fmtNum(item.don_lo, d)}`),
        el("span", {}, `Close: ${fmtNum(item.close, d)} (${item.channel_pct}%)`),
        el("span", {}, `Long Trigger: ${fmtNum(item.don_hi, d)}`)
      );
      gauge.append(track, labels);
      card.appendChild(gauge);

      const metrics = el("div", { class: "radar-metrics" });
      metrics.append(
        el("div", {}, `Bid / Ask: ${fmtNum(item.bid, d)} / ${fmtNum(item.ask, d)}`),
        el("div", {}, `1H / 4H Trend: ${item.trend_1h} / ${item.trend_4h}`),
        el("div", {}, `To Long: ${fmtNum(item.dist_to_long, d)} pts`),
        el("div", {}, `To Short: ${fmtNum(item.dist_to_short, d)} pts`),
        el("div", {}, `ATR(14): ${fmtNum(item.atr14, d)} (SL ${fmtNum(item.sl_dist, d)})`),
        el("div", {}, `Trail Dist (2.5×): ${fmtNum(item.trail_dist, d)}`),
        el("div", {}, `Est. 1% Lots: ${fmtNum(item.est_lots_1pct, 2)}`),
        el("div", { class: item.spread_ok ? "pos" : "neg" }, `Spread: ${fmtMoney(item.spread_cost_usd)} (${fmtNum(item.spread_ratio_pct, 1)}% risk)`)
      );
      card.appendChild(metrics);

      const chartWrap = el("div", { class: "canvas-wrap radar-chart-wrap" });
      const canvas = el("canvas", {
        id: `radar-canvas-${item.symbol}`,
        "aria-label": `${item.symbol} 1H Donchian channel chart`,
      });
      chartWrap.appendChild(canvas);
      card.appendChild(chartWrap);
      grid.appendChild(card);
    }

    if (state.activeTab === "radar") {
      requestAnimationFrame(() => renderRadarCharts(radarItems));
    }
  }

  function renderReports(strategies, snap) {
    const cmpGrid = $("benchmark-compare-grid");
    clearChildren(cmpGrid);
    const livePerf = snap?.performance || {};
    const don = strategies.find((s) => s.strategy === "DONCHIAN_1H_TREND") || {
      "wr%": "37.85",
      pf: "1.3",
      net_pnl: "201908.73",
      "dd%": "18.28",
      trades: "819",
      "+yrs": "4/4",
    };

    const cards = [
      ["4Y Backtest Trades", `${don.trades} (NQ + XAUUSD)`, `Live: ${livePerf.total_trades || 0} trades`],
      ["4Y Backtest Win Rate", `${don["wr%"]}%`, `Live: ${fmtNum(livePerf.win_rate_pct || 0, 1)}%`],
      ["4Y Profit Factor", `${don.pf}`, `Live: ${fmtNum(livePerf.profit_factor || 0, 2)}`],
      ["4Y Net Profit ($100k)", fmtMoney(don.net_pnl), `Live: ${fmtMoney(livePerf.net_pnl || 0, true)}`],
      ["4Y Max Drawdown", `${don["dd%"]}%`, `Live: ${fmtNum(livePerf.max_drawdown_pct || 0, 2)}%`],
      ["Annual Consistency", `${don["+yrs"] || "4/4"} green years`, "2023–2026 verified"],
    ];
    for (const [label, val, sub] of cards) {
      const c = el("article", { class: "kpi-card" });
      c.append(el("span", { class: "kpi-label" }, label), el("strong", { class: "kpi-value" }, val), el("span", { class: "kpi-sub" }, sub));
      cmpGrid.appendChild(c);
    }

    const tbody = $("reports-tbody");
    clearChildren(tbody);
    for (const row of strategies) {
      const tr = el("tr", {
        class: row.strategy === "DONCHIAN_1H_TREND" ? "highlight-row" : "",
      });
      const net = Number(row.net_pnl || 0);
      tr.append(
        el("th", { scope: "row", dataset: { value: row.strategy } }, row.strategy),
        el("td", { dataset: { value: row.trades } }, String(row.trades)),
        el("td", { dataset: { value: row["wr%"] } }, `${row["wr%"]}%`),
        el("td", { dataset: { value: row.pf } }, String(row.pf)),
        el("td", { class: net >= 0 ? "pos" : "neg", dataset: { value: net } }, fmtMoney(net, true)),
        el("td", { dataset: { value: row["dd%"] } }, `${row["dd%"]}%`),
        el("td", { dataset: { value: row["ret/dd"] } }, String(row["ret/dd"])),
        el("td", { dataset: { value: row["+yrs"] } }, String(row["+yrs"]))
      );
      tbody.appendChild(tr);
    }
  }

  function renderLogs(lines, bot) {
    const sub = $("log-heartbeat-sub");
    if (bot?.present) {
      const age = bot.age_seconds != null ? `${bot.age_seconds}s ago` : "unknown age";
      sub.textContent = `${bot.alive ? "Bot heartbeat active" : "Bot inactive"} (PID ${bot.pid || "—"}, last poll ${age})`;
    } else {
      sub.textContent = "live_state.json not created yet — start live_mt5_bot.py to publish heartbeat.";
    }

    const mode = $("log-filter").value;
    const filtered = lines.filter((item) => {
      if (mode === "SIGNALS") {
        return /SIGNAL|ORDER|TRAIL|DRY-RUN/i.test(item.raw);
      }
      if (mode === "WARN") {
        return item.level === "WARNING" || item.level === "ERROR";
      }
      return true;
    });

    const feed = $("log-feed");
    clearChildren(feed);
    if (!filtered.length) {
      feed.appendChild(el("p", { class: "empty-note" }, "No matching log entries."));
      return;
    }

    for (const item of filtered) {
      const isSig = /SIGNAL|ORDER|TRAIL/i.test(item.msg);
      const row = el("div", {
        class: `log-row lvl-${item.level} ${isSig ? "is-signal" : ""}`,
      });
      if (item.ts) row.appendChild(el("span", { class: "log-ts" }, item.ts));
      row.appendChild(el("span", { class: "pill neutral" }, item.level));
      row.appendChild(el("span", {}, item.msg));
      feed.appendChild(row);
    }
  }

  // Canvas 2D Charts (DPI-aware, zero inline script/styles needed)
  function prepCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return null;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w: rect.width, h: rect.height };
  }

  function drawEquityChart(curve, initialBalance) {
    const canvas = $("equity-canvas");
    const setup = prepCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);
    state.equityPoints = [];

    if (!curve || !curve.length) return;
    const vals = curve.map((p) => Number(p.equity));
    const minV = Math.min(...vals, initialBalance || vals[0]);
    const maxV = Math.max(...vals, initialBalance || vals[0]);
    const span = Math.max(100, (maxV - minV) * 1.15);
    const lo = (minV + maxV) / 2 - span / 2;
    const hi = lo + span;

    const pad = { top: 18, right: 16, bottom: 26, left: 68 };
    const cw = Math.max(10, w - pad.left - pad.right);
    const ch = Math.max(10, h - pad.top - pad.bottom);
    const xAt = (i) => pad.left + (curve.length === 1 ? cw / 2 : (i / (curve.length - 1)) * cw);
    const yAt = (v) => pad.top + ((hi - v) / (hi - lo)) * ch;

    // Grid lines
    ctx.strokeStyle = "#243049";
    ctx.lineWidth = 1;
    ctx.fillStyle = "#93a3c0";
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let g = 0; g <= 3; g += 1) {
      const val = lo + (g / 3) * (hi - lo);
      const y = yAt(val);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(w - pad.right, y);
      ctx.stroke();
      ctx.fillText(fmtMoney(val), pad.left - 8, y);
    }

    // Starting capital dashed baseline
    const baseY = yAt(initialBalance || curve[0].equity);
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "#475569";
    ctx.beginPath();
    ctx.moveTo(pad.left, baseY);
    ctx.lineTo(w - pad.right, baseY);
    ctx.stroke();
    ctx.restore();

    const lastEq = vals[vals.length - 1];
    const lineColor = lastEq >= (initialBalance || vals[0]) ? "#38bdf8" : "#f87171";

    // Area fill
    ctx.beginPath();
    curve.forEach((pt, idx) => {
      const x = xAt(idx);
      const y = yAt(pt.equity);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
      state.equityPoints.push({ x, y, ...pt });
    });
    ctx.lineTo(xAt(curve.length - 1), h - pad.bottom);
    ctx.lineTo(xAt(0), h - pad.bottom);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, pad.top, 0, h - pad.bottom);
    grad.addColorStop(0, "rgba(56, 189, 248, 0.28)");
    grad.addColorStop(1, "rgba(56, 189, 248, 0.01)");
    ctx.fillStyle = grad;
    ctx.fill();

    // Curve stroke & dots
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2.2;
    ctx.beginPath();
    state.equityPoints.forEach((pt, idx) => {
      if (idx === 0) ctx.moveTo(pt.x, pt.y);
      else ctx.lineTo(pt.x, pt.y);
    });
    ctx.stroke();

    for (const pt of state.equityPoints) {
      ctx.fillStyle = Number(pt.pnl) < 0 ? "#f87171" : "#38bdf8";
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, curve.length > 40 ? 2 : 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function renderRadarCharts(radarItems) {
    for (const item of radarItems) {
      if (item.status !== "READY" || !item.bars?.length) continue;
      const canvas = $(`radar-canvas-${item.symbol}`);
      if (canvas) drawRadarCandles(canvas, item);
    }
  }

  function drawRadarCandles(canvas, item) {
    const setup = prepCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    ctx.clearRect(0, 0, w, h);

    const bars = item.bars;
    const highs = bars.flatMap((b) => [b.h, b.don_hi ?? b.h]);
    const lows = bars.flatMap((b) => [b.l, b.don_lo ?? b.l]);
    const minP = Math.min(...lows);
    const maxP = Math.max(...highs);
    const span = Math.max(1e-6, (maxP - minP) * 1.12);
    const lo = (minP + maxP) / 2 - span / 2;
    const hi = lo + span;

    const pad = { top: 10, right: 12, bottom: 18, left: 62 };
    const cw = Math.max(10, w - pad.left - pad.right);
    const ch = Math.max(10, h - pad.top - pad.bottom);
    const step = cw / bars.length;
    const xAt = (i) => pad.left + (i + 0.5) * step;
    const yAt = (p) => pad.top + ((hi - p) / (hi - lo)) * ch;

    ctx.strokeStyle = "#1f2a42";
    ctx.fillStyle = "#93a3c0";
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let g = 0; g <= 2; g += 1) {
      const pv = lo + (g / 2) * (hi - lo);
      const y = yAt(pv);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(w - pad.right, y);
      ctx.stroke();
      ctx.fillText(fmtNum(pv, item.digits || 2), pad.left - 6, y);
    }

    // Helper to draw overlay lines
    const drawLine = (key, color, dashed = false) => {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      if (dashed) ctx.setLineDash([4, 3]);
      ctx.beginPath();
      let started = false;
      bars.forEach((b, i) => {
        if (b[key] == null) return;
        const x = xAt(i);
        const y = yAt(b[key]);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();
      ctx.restore();
    };

    drawLine("don_hi", "#22c55e", true);
    drawLine("don_lo", "#f87171", true);
    drawLine("ema50", "#f59e0b", false);

    // Candles
    const bodyW = Math.max(2, step * 0.58);
    bars.forEach((b, i) => {
      const x = xAt(i);
      const bull = b.c >= b.o;
      const color = bull ? "#22c55e" : "#f87171";
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, yAt(b.h));
      ctx.lineTo(x, yAt(b.l));
      ctx.stroke();

      const topY = yAt(Math.max(b.o, b.c));
      const botY = yAt(Math.min(b.o, b.c));
      ctx.fillRect(x - bodyW / 2, topY, bodyW, Math.max(1.5, botY - topY));
    });
  }

  function setupInteractions() {
    $("refresh-btn").addEventListener("click", () => refreshAll(true));
    $("reload-logs-btn").addEventListener("click", () => loadLogs());
    $("log-filter").addEventListener("change", () => renderLogs(state.logs, state.snapshot?.bot));
    $("trade-symbol-filter").addEventListener("change", () => {
      if (state.snapshot) renderTradesTable(state.snapshot.trades || []);
    });

    const scopeAll = $("scope-all-btn");
    const scopeBot = $("scope-bot-btn");
    const setScope = (nextScope) => {
      if (state.scope === nextScope) return;
      state.scope = nextScope;
      scopeAll.setAttribute("aria-pressed", String(nextScope === "all"));
      scopeBot.setAttribute("aria-pressed", String(nextScope === "bot"));
      refreshAll(false);
    };
    scopeAll.addEventListener("click", () => setScope("all"));
    scopeBot.addEventListener("click", () => setScope("bot"));

    // Interactive touch/hover inspection on Equity Canvas
    const eqCanvas = $("equity-canvas");
    const inspectPoint = (clientX) => {
      const pts = state.equityPoints;
      if (!pts.length) return;
      const rect = eqCanvas.getBoundingClientRect();
      const x = clientX - rect.left;
      let nearest = pts[0];
      let bestDist = Infinity;
      for (const p of pts) {
        const d = Math.abs(p.x - x);
        if (d < bestDist) {
          bestDist = d;
          nearest = p;
        }
      }
      $("equity-tooltip").textContent = `${nearest.label} · ${nearest.symbol} · Eq ${fmtMoney(nearest.equity)} (${fmtMoney(nearest.pnl, true)})`;
    };
    eqCanvas.addEventListener("pointermove", (e) => inspectPoint(e.clientX));
    eqCanvas.addEventListener("pointerdown", (e) => inspectPoint(e.clientX));

    // Redraw canvases cleanly on viewport rotation/resize
    let resizeTick = null;
    window.addEventListener("resize", () => {
      window.clearTimeout(resizeTick);
      resizeTick = window.setTimeout(() => {
        if (!state.snapshot) return;
        if (state.activeTab === "overview") {
          drawEquityChart(state.snapshot.performance.equity_curve, state.snapshot.performance.initial_balance);
        } else if (state.activeTab === "radar") {
          renderRadarCharts(state.snapshot.radar || []);
        }
      }, 120);
    });

    setupChartControls();
  }

  function openPositionOnChart(pos) {
    $("tab-chart").click();
    window.TVStudio?.openSymbol(pos.symbol, "1h", null);
  }

  function inspectClosedTradeOnChart(trade) {
    const ageHours = (Date.now() / 1000 - Number(trade.entry_epoch || 0)) / 3600;
    const bestTf = ageHours > 240 ? "4h" : "1h";
    $("tab-chart").click();
    window.TVStudio?.openSymbol(trade.symbol, bestTf, trade);
  }

  function setupChartControls() {
    window.TVStudio?.init({
      apiJson,
      onStatusAlert: (msg) => showAlert(msg),
    });
  }

  function setupInstallSupport() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }

    const installButton = $("install-app-btn");
    const installStatus = $("install-status");
    if (!installButton || !installStatus) return;

    const supportsWebInstall = typeof Navigator.prototype.install === "function";
    let deferredInstallPrompt;

    if (supportsWebInstall) {
      installButton.hidden = false;
    }

    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      deferredInstallPrompt = event;
      if (!supportsWebInstall) {
        installButton.hidden = false;
      }
    });

    window.addEventListener("appinstalled", () => {
      deferredInstallPrompt = undefined;
      installButton.hidden = true;
      installStatus.textContent = "TradeTerminal was installed on this device.";
      installStatus.hidden = false;
    });

    installButton.addEventListener("click", async () => {
      if (installButton.getAttribute("aria-disabled") === "true") return;
      installButton.setAttribute("aria-disabled", "true");
      try {
        if (supportsWebInstall) {
          await navigator.install();
          installStatus.textContent = "The app was installed or opened.";
          installStatus.hidden = false;
          installButton.hidden = true;
        } else if (deferredInstallPrompt) {
          const promptEvent = deferredInstallPrompt;
          deferredInstallPrompt = undefined;
          await promptEvent.prompt();
          const { outcome } = await promptEvent.userChoice;
          installStatus.textContent =
            outcome === "accepted"
              ? "TradeTerminal was installed."
              : "Installation canceled.";
          installStatus.hidden = false;
          if (!deferredInstallPrompt) {
            installButton.hidden = true;
          }
        }
      } catch (err) {
        if (err && err.name === "AbortError") {
          installStatus.textContent = "Installation was canceled or could not complete.";
        } else {
          installStatus.textContent = "Installation could not start in this browser context.";
        }
        installStatus.hidden = false;
      } finally {
        installButton.removeAttribute("aria-disabled");
      }
    });
  }

  function setupDevicesDialog() {
    const dialog = $("phone-connect-dialog");
    const openBtn = $("devices-btn");
    const urlList = $("devices-url-list");
    const copyStatus = $("copy-url-status");
    if (!dialog || !openBtn || !urlList || !copyStatus) return;

    // Fallback for browsers without native dialog closedBy="any" support
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (e) => {
        const r = dialog.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      let pressedOutside = false;
      dialog.addEventListener("pointerdown", (e) => {
        pressedOutside = outside(e);
      });
      dialog.addEventListener("pointerup", (e) => {
        if (pressedOutside && outside(e)) dialog.close();
      });
    }

    dialog.addEventListener("close", () => {
      dialog.returnValue = "";
    });

    const addUrlRow = (label, url) => {
      const row = el("div", { class: "url-row" });
      const meta = el("div", { class: "url-meta" });
      meta.append(el("span", { class: "url-label" }, label), el("span", { class: "url-text" }, url));

      const copyBtn = el("button", { type: "button", class: "btn-mini" }, "Copy");
      copyBtn.addEventListener("click", async () => {
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(url);
            copyStatus.textContent = `Copied ${label}: ${url}`;
          } else {
            copyStatus.textContent = `Copy manually: ${url}`;
          }
        } catch (_) {
          copyStatus.textContent = `Copy manually: ${url}`;
        }
        copyStatus.hidden = false;
      });

      row.append(meta, copyBtn);
      urlList.appendChild(row);
    };

    openBtn.addEventListener("click", async () => {
      copyStatus.hidden = true;
      copyStatus.textContent = "";
      urlList.textContent = "Checking network URLs...";
      dialog.showModal();

      try {
        const info = await apiJson("/api/connection-info");
        urlList.textContent = "";
        addUrlRow("Current Browser URL", window.location.origin);
        for (const u of info.urls?.lan || []) addUrlRow("Home Wi-Fi (Phone / Second Laptop)", u);
        for (const u of info.urls?.tailscale || []) addUrlRow("Tailscale (4G / 5G Anywhere)", u);
        if (!(info.urls?.lan || []).length && !(info.urls?.tailscale || []).length) {
          for (const u of info.urls?.local || []) addUrlRow("Local PC Loopback", u);
        }
      } catch (err) {
        urlList.textContent = `Could not load URLs: ${err.message}`;
      }
    });
  }

  async function boot() {
    setupAuthForm();
    setupTabs();
    setupSortableTable("trades-table", "trades-sort-status");
    setupSortableTable("reports-table", "reports-sort-status");
    setupInteractions();
    setupInstallSupport();
    setupDevicesDialog();

    try {
      const auth = await apiJson("/api/auth");
      if (!auth.auth_required || auth.authenticated) {
        showAppView();
      } else {
        showAuthView();
      }
    } catch (err) {
      if (isNetworkFetchError(err)) {
        showAuthView("TradeTerminal Server is offline. Double-click Launch_TradeTerminal.bat (or run terminal_server.py) first, then enter your passcode.");
      } else {
        showAuthView();
      }
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
