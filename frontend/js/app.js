/* ──────────────────────────────────────────────────────────────────────────
   pump-chart  ·  Price Action + Strategy Dashboard
   ────────────────────────────────────────────────────────────────────────── */

const WS_BASE = `ws://${location.host}/ws`;
const MAX_TRADES = 60;
const RECONNECT_MS = 1500;
const CANDLE_UP = "#26a69a";
const CANDLE_DOWN = "#ef5350";
const CANDLE_FLAT = "#5a6071";

/* Colors for indicators */
const EMA_FAST_COLOR = "#00e5ff";  // Cyan
const EMA_SLOW_COLOR = "#ff9800";  // Orange
const ATR_COLOR = "#ef5350";  // Red
const ROC_COLOR = "#64b5f6";  // Light blue
const ROC_SIGNAL_COLOR = "#ff9800"; // Orange for signal line

/* Regime colors */
const REGIME_COLORS = {
  idle: "#5a6071",
  trend: "#26a69a",
  exhaustion: "#ff9800",
  reversal: "#ef5350",
  continuation: "#7c4dff",
};

let chart, candleSeries, volSeries, ws, reconnectTimer;
let emaFastSeries, emaSlowSeries;  // EMA overlay lines
let atrSeries, rocSeries;           // Sub-pane series
let currentMint = "", currentTf = "1m";
let reconnectMs = RECONNECT_MS;
let openPrice = null, lastClose = null;
let initMcapSol = 0, initMcapUsd = 0;
let currentPrecision = 6;
let tfSeconds = 60;
let lastCandleTime = null;
let lastCandleClose = null;
let chartBasePrice = null;
let chartBaseMcap = null;
let chartCurrency = "USD";
let liveMcapCandle = null;
const LIVE_ONLY_MARKETCAP = true;

/* Strategy state */
let strategyResults = [];
let volumeProfileOverlays = [];
let markers = [];
let lastRegime = "idle";
let forwardTestStats = null;
let pendingMarkerData = [];  // raw marker data awaiting market cap resolution

/* Strategy Engine Parameters */
let engineParams = {
  ema_fast: 3, ema_slow: 7, atr_period: 7, roc_period: 3, warmup: 20,
  signal_strong: 2.0, signal_weak: 1.2, signal_noise: 1.0,
  exhaustion_bars_limit: 7, delta_threshold: 0.3, kalman_gamma: 0.1,
  min_trend_bars: 3, reversal_confirm_bars: 2, chop_atr_pct: 0.5,
  chop_spread_pct: 0.15, reversal_exit_confirm_bars: 1,
  s_effective_threshold: 0.5, exhaustion_persist_bars: 2,
  regime_lookback: 5, persistence_threshold: 3, momentum_mean_threshold: 0.0,
  ema_min_spread_pct: 0.05, confidence_high: 0.60, confidence_low: 0.35,
  confidence_w1: 0.30, confidence_w2: 0.25, confidence_w3: 0.25, confidence_w4: 0.20,
  atr_floor_k: 0.6, ema_cross_persist_bars: 2, exhaustion_s_decay_bars: 2,
  local_range_bars: 10, local_range_threshold_pct: 0.3, sign_flip_threshold: 4,
  stability_bars: 2,
};

const $ = id => document.getElementById(id);
const mintInput = $("mint-input");
const loadBtn = $("load-btn");
const tfBtns = document.querySelectorAll(".tf-btn");
const dot = $("dot");
const connLabel = $("conn-label");
const tokenBar = $("token-bar");
const tokenLogo = $("token-logo");
const tokenName = $("token-name");
const tokenSymbol = $("token-symbol");
const lastPriceEl = $("last-price");
const priceChange = $("price-change");
const mcapEl = $("stat-mcap");
const volEl = $("stat-vol");
const ohlcOpenEl = $("ohlc-open");
const ohlcHighEl = $("ohlc-high");
const ohlcLowEl = $("ohlc-low");
const ohlcCloseEl = $("ohlc-close");
const tradeFeed = $("trade-feed");
const overlay = $("overlay");
const overlayIcon = $("overlay-icon");
const overlayMsg = $("overlay-msg");
const settingsBtn = $("settings-btn");
const settingsModal = $("settings-modal");
const closeSettingsBtn = $("close-settings");
const applySettingsBtn = $("apply-settings-btn");
const settingsForm = $("settings-form");

/* ── Chart init ──────────────────────────────────────────────────────── */

function initChart() {
  const wrapper = $("chart");
  if (chart) chart.remove();
  chart = LightweightCharts.createChart(wrapper, {
    layout: { background: { color: "#0d0f12" }, textColor: "#5a6071" },
    grid: { vertLines: { color: "#1e2330" }, horzLines: { color: "#1e2330" } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal, vertLine: { color: "#5865f2", labelBackgroundColor: "#5865f2" }, horzLine: { color: "#5865f2", labelBackgroundColor: "#5865f2" } },
    timeScale: { borderColor: "#1e2330", timeVisible: true, secondsVisible: true, rightBarStaysOnScroll: true, shiftVisibleRangeOnNewBar: true },
    rightPriceScale: { borderColor: "#1e2330", scaleMargins: { top: 0.12, bottom: 0.28 } },
    handleScroll: { mouseWheel: true, pressedMouseMove: true },
    handleScale: { mouseWheel: true, pinch: true },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: "#26a69a", downColor: "#ef5350",
    borderUpColor: "#26a69a", borderDownColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    priceFormat: {
      type: "custom",
      minMove: 1,
      formatter: v => formatMcap(v),
    },
  });

  /* EMA overlay lines */
  emaFastSeries = chart.addLineSeries({
    color: EMA_FAST_COLOR,
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
    priceFormat: { type: "custom", minMove: 1, formatter: v => formatMcap(v) },
  });

  emaSlowSeries = chart.addLineSeries({
    color: EMA_SLOW_COLOR,
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
    priceFormat: { type: "custom", minMove: 1, formatter: v => formatMcap(v) },
  });

  /* Volume histogram */
  volSeries = chart.addHistogramSeries({
    color: "#5865f222", priceFormat: { type: "volume" }, priceScaleId: "volume",
  });
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: wrapper.clientWidth, height: wrapper.clientHeight });
  });
  ro.observe(wrapper);
  chart.applyOptions({ width: wrapper.clientWidth, height: wrapper.clientHeight });
}

/* ── Formatting helpers ──────────────────────────────────────────────── */

function formatPrice(p) {
  if (!p || p === 0) return "—";
  if (p >= 0.001) return p.toFixed(6);
  return p.toExponential(4);
}

function formatMcap(v) {
  if (!v || v <= 0) return "—";
  const prefix = chartCurrency === "USD" ? "$" : "";
  const suffix = chartCurrency === "SOL" ? " SOL" : "";
  return prefix + fmtLarge(v) + suffix;
}

function timeframeToSeconds(tf) {
  const m = { "1s": 1, "5s": 5, "15s": 15, "1m": 60, "5m": 300, "15m": 900, "1h": 3600 };
  return m[tf] || 60;
}

function fmtLarge(n) {
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + "B";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function precisionForPrice(price) {
  if (!price || price <= 0) return 6;
  if (price >= 1000) return 2;
  if (price >= 1) return 4;
  const decimals = Math.ceil(-Math.log10(price)) + 2;
  return Math.max(6, Math.min(12, decimals));
}

function syncPriceScale(price) {
  if (!candleSeries) return;
  candleSeries.applyOptions({
    priceFormat: { type: "custom", minMove: 1, formatter: v => formatMcap(v) },
  });
}

function ensureMcapBase(price) {
  if (!chartBasePrice && price > 0) chartBasePrice = price;
  if (!chartBaseMcap) {
    if (initMcapUsd > 0) { chartBaseMcap = initMcapUsd; chartCurrency = "USD"; }
    else if (initMcapSol > 0) { chartBaseMcap = initMcapSol; chartCurrency = "SOL"; }
  }
}

function toMarketCapValue(price) {
  ensureMcapBase(price);
  if (chartBasePrice && chartBaseMcap && price > 0) {
    return chartBaseMcap * (price / chartBasePrice);
  }
  return 0;
}

function candleToMcap(candle) {
  return {
    time: candle.time,
    open: toMarketCapValue(candle.open),
    high: toMarketCapValue(candle.high),
    low: toMarketCapValue(candle.low),
    close: toMarketCapValue(candle.close),
    volume: candle.volume || 0,
  };
}

function candleToMcapFromCloseAnchor(candle, closeMcap) {
  if (!candle || !closeMcap || candle.close <= 0) return candleToMcap(candle);
  const factor = closeMcap / candle.close;
  return {
    time: candle.time,
    open: candle.open * factor,
    high: candle.high * factor,
    low: candle.low * factor,
    close: candle.close * factor,
    volume: candle.volume || 0,
  };
}

function buildMcapCandleFromTick(time, closeMcap, volume = 0, isNew = false) {
  if (!time || !closeMcap || closeMcap <= 0) return null;
  if (!liveMcapCandle || isNew || liveMcapCandle.time !== time) {
    const open = lastCandleClose !== null ? lastCandleClose : closeMcap;
    liveMcapCandle = {
      time, open,
      high: Math.max(open, closeMcap),
      low: Math.min(open, closeMcap),
      close: closeMcap,
      volume: volume || 0,
    };
  } else {
    liveMcapCandle.close = closeMcap;
    if (closeMcap > liveMcapCandle.high) liveMcapCandle.high = closeMcap;
    if (closeMcap < liveMcapCandle.low) liveMcapCandle.low = closeMcap;
    liveMcapCandle.volume = volume || liveMcapCandle.volume || 0;
  }
  return { ...liveMcapCandle };
}

function withCandleColors(candle, prevClose = null) {
  let color = CANDLE_FLAT;
  if (candle.close > candle.open) color = CANDLE_UP;
  else if (candle.close < candle.open) color = CANDLE_DOWN;
  else if (prevClose !== null) {
    if (candle.close > prevClose) color = CANDLE_UP;
    else if (candle.close < prevClose) color = CANDLE_DOWN;
  }
  return { ...candle, color, borderColor: color, wickColor: color };
}

function updateOhlc(candle) {
  if (!candle) return;
  if (ohlcOpenEl) ohlcOpenEl.textContent = formatMcap(candle.open);
  if (ohlcHighEl) ohlcHighEl.textContent = formatMcap(candle.high);
  if (ohlcLowEl) ohlcLowEl.textContent = formatMcap(candle.low);
  if (ohlcCloseEl) ohlcCloseEl.textContent = formatMcap(candle.close);
}

function pushCandleWithContinuity(candle) {
  if (!candleSeries || !candle) return;
  if (lastCandleTime !== null && lastCandleClose !== null && candle.time > lastCandleTime + tfSeconds) {
    const gapCandles = Math.floor((candle.time - lastCandleTime) / tfSeconds) - 1;
    // Only fill small gaps (≤ 3 missed candles). Larger gaps = stale/flat period,
    // just jump to avoid flooding the chart with ghost candles when data resumes.
    if (gapCandles <= 3) {
      for (let t = lastCandleTime + tfSeconds; t < candle.time; t += tfSeconds) {
        const flat = { time: t, open: lastCandleClose, high: lastCandleClose, low: lastCandleClose, close: lastCandleClose, volume: 0 };
        candleSeries.update(withCandleColors(flat, lastCandleClose));
        updateOhlc(flat);
        lastCandleTime = t;
        lastCandleClose = flat.close;
      }
    }
  }
  candleSeries.update(withCandleColors(candle, lastCandleClose));
  updateOhlc(candle);
  lastCandleTime = candle.time;
  lastCandleClose = candle.close;
}

/* ── UI state helpers ────────────────────────────────────────────────── */

function setDot(state, label) {
  dot.className = "dot " + state;
  connLabel.textContent = label;
}

function showOverlay(icon, msg) {
  overlay.classList.remove("hidden");
  overlayIcon.textContent = icon;
  overlayMsg.textContent = msg;
}
function hideOverlay() { overlay.classList.add("hidden"); }

function updatePrice(price) {
  lastClose = price;
  syncPriceScale(price);
  lastPriceEl.textContent = formatMcap(price);
  if (openPrice !== null && openPrice > 0) {
    const pct = ((price - openPrice) / openPrice) * 100;
    priceChange.textContent = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
    priceChange.className = "price-change " + (pct >= 0 ? "pos" : "neg");
  }
}

function addTrade(trade) {
  const row = document.createElement("div");
  row.className = `trade-row ${trade.tx_type}`;
  const t = document.createElement("span"); t.className = `trade-type ${trade.tx_type}`; t.textContent = trade.tx_type;
  const s = document.createElement("span"); s.className = "trade-sol"; s.textContent = trade.sol_amount.toFixed(3) + " SOL";
  const tm = document.createElement("span"); tm.className = "trade-time"; tm.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  row.append(t, s, tm);
  tradeFeed.prepend(row);
  while (tradeFeed.children.length > MAX_TRADES) tradeFeed.lastElementChild.remove();
}

/* ── Strategy rendering ──────────────────────────────────────────────── */

function updateStrategyPanel(result) {
  if (!result) return;

  /* Regime badge */
  const regimeEl = $("regime-badge");
  if (regimeEl) {
    regimeEl.textContent = result.regime.toUpperCase();
    regimeEl.style.background = REGIME_COLORS[result.regime] || "#5a6071";
    regimeEl.className = "regime-badge";
  }

  /* Direction */
  const dirEl = $("direction-badge");
  if (dirEl) {
    dirEl.textContent = result.direction === "up" ? "▲ UP" : result.direction === "down" ? "▼ DOWN" : "— NONE";
    dirEl.style.color = result.direction === "up" ? CANDLE_UP : result.direction === "down" ? CANDLE_DOWN : "#5a6071";
  }

  /* Signal strength (S) */
  const sEl = $("signal-strength");
  if (sEl && result.indicators) {
    const s = result.indicators.signal_strength || 0;
    sEl.textContent = s.toFixed(2);
    sEl.style.color = s > 1.5 ? CANDLE_UP : s < 0.8 ? CANDLE_DOWN : "#ff9800";
  }

  /* Indicators */
  const indEl = $("indicators-display");
  if (indEl && result.indicators) {
    const ind = result.indicators;
    indEl.innerHTML = `
      <div class="ind-row"><span class="ind-label" style="color:${EMA_FAST_COLOR}">EMA ${5}</span><span class="ind-val">${ind.ema_fast ? formatMcap(toMarketCapValue(ind.ema_fast)) : "—"}</span></div>
      <div class="ind-row"><span class="ind-label" style="color:${EMA_SLOW_COLOR}">EMA ${13}</span><span class="ind-val">${ind.ema_slow ? formatMcap(toMarketCapValue(ind.ema_slow)) : "—"}</span></div>
      <div class="ind-row"><span class="ind-label" style="color:${ATR_COLOR}">ATR 10</span><span class="ind-val">${ind.atr ? ind.atr.toExponential(3) : "—"}</span></div>
      <div class="ind-row"><span class="ind-label" style="color:${ROC_COLOR}">ROC 5</span><span class="ind-val">${ind.roc ? ind.roc.toFixed(3) : "—"}</span></div>
      <div class="ind-row"><span class="ind-label">Spread</span><span class="ind-val ${ind.spread_expanding ? "pos" : "neg"}">${ind.ema_spread ? ind.ema_spread.toExponential(3) : "—"} ${ind.spread_expanding ? "▲" : "▼"}</span></div>
    `;
  }

  /* Forward test stats */
  if (result.forward_test) {
    const ft = result.forward_test;
    const statsEl = $("ft-stats");
    if (statsEl) {
      const stats = ft.stats;
      const pnlColor = ft.unrealized_pnl >= 0 ? "pos" : "neg";
      const totalPnlColor = stats.total_pnl_sol >= 0 ? "pos" : "neg";
      statsEl.innerHTML = `
        <div class="ft-row"><span class="ft-label">Balance</span><span class="ft-val">${ft.balance.toFixed(4)} SOL</span></div>
        <div class="ft-row"><span class="ft-label">Trades</span><span class="ft-val">${stats.total_trades}</span></div>
        <div class="ft-row"><span class="ft-label">Win Rate</span><span class="ft-val">${stats.win_rate.toFixed(1)}%</span></div>
        <div class="ft-row"><span class="ft-label">PnL</span><span class="ft-val ${totalPnlColor}">${stats.total_pnl_sol >= 0 ? "+" : ""}${stats.total_pnl_sol.toFixed(4)} SOL</span></div>
        <div class="ft-row"><span class="ft-label">Fees Paid</span><span class="ft-val neg">${stats.total_fees_paid.toFixed(4)} SOL</span></div>
        <div class="ft-row"><span class="ft-label">Max DD</span><span class="ft-val neg">${stats.max_drawdown_pct.toFixed(2)}%</span></div>
        ${ft.current_trade ? `
          <div class="ft-divider"></div>
          <div class="ft-row"><span class="ft-label">Position</span><span class="ft-val">${ft.current_trade.direction.toUpperCase()}</span></div>
          <div class="ft-row"><span class="ft-label">Unreal. PnL</span><span class="ft-val ${pnlColor}">${ft.unrealized_pnl >= 0 ? "+" : ""}${ft.unrealized_pnl.toFixed(4)} (${ft.unrealized_pnl_pct >= 0 ? "+" : ""}${ft.unrealized_pnl_pct.toFixed(2)}%)</span></div>
        ` : ""}
      `;
    }
  }
}

/* ── Volume Profile Canvas Overlay ───────────────────────────────────── */

let vpCanvas = null;
let vpCtx = null;

function initVolumeProfileCanvas() {
  const wrapper = $("chart");
  // Remove existing canvas if any
  const existing = wrapper.querySelector(".vp-canvas");
  if (existing) existing.remove();

  vpCanvas = document.createElement("canvas");
  vpCanvas.className = "vp-canvas";
  vpCanvas.style.cssText = "position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:5;";
  wrapper.appendChild(vpCanvas);
  vpCtx = vpCanvas.getContext("2d");

  function resizeVP() {
    const dpr = window.devicePixelRatio || 1;
    vpCanvas.width = wrapper.clientWidth * dpr;
    vpCanvas.height = wrapper.clientHeight * dpr;
    vpCanvas.style.width = wrapper.clientWidth + "px";
    vpCanvas.style.height = wrapper.clientHeight + "px";
    vpCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    renderVolumeProfiles();
  }

  const ro = new ResizeObserver(resizeVP);
  ro.observe(wrapper);
  // Initial sizing
  setTimeout(resizeVP, 50);
}

function renderVolumeProfiles() {
  if (!vpCtx || !vpCanvas || !chart || !candleSeries) return;

  const dpr = window.devicePixelRatio || 1;
  const w = vpCanvas.width / dpr;
  const h = vpCanvas.height / dpr;
  vpCtx.clearRect(0, 0, w, h);

  if (!strategyResults.length) return;

  // Get the last strategy result's volume profiles
  const lastResult = strategyResults[strategyResults.length - 1];
  if (!lastResult || !lastResult.volume_profiles) return;

  const profiles = lastResult.volume_profiles;
  const ts = chart.timeScale();

  for (const profile of profiles) {
    if (!profile.bins || profile.bins.length === 0) continue;
    if (!profile.start_time || !profile.end_time) continue;

    // Convert times to x coordinates
    const x1Coord = ts.timeToCoordinate(profile.start_time);
    const x2Coord = ts.timeToCoordinate(profile.end_time);
    if (x1Coord === null || x2Coord === null) continue;

    const profileWidth = Math.max(Math.abs(x2Coord - x1Coord), 40);
    const xStart = Math.min(x1Coord, x2Coord);

    // Find max volume for scaling
    const maxVol = Math.max(...profile.bins.map(b => b.total_volume), 0.001);

    // Draw each bin as horizontal bar
    for (const bin of profile.bins) {
      if (bin.total_volume <= 0) continue;

      const priceMid = (bin.price_low + bin.price_high) / 2;
      const mcapLow = toMarketCapValue(bin.price_low);
      const mcapHigh = toMarketCapValue(bin.price_high);

      const yLow = candleSeries.priceToCoordinate(mcapLow);
      const yHigh = candleSeries.priceToCoordinate(mcapHigh);
      if (yLow === null || yHigh === null) continue;

      const barHeight = Math.max(Math.abs(yHigh - yLow), 1);
      const yTop = Math.min(yLow, yHigh);

      // Buy volume (green bar from left)
      const buyWidth = (bin.buy_volume / maxVol) * profileWidth * 0.9;
      if (buyWidth > 0.5) {
        vpCtx.fillStyle = "rgba(38, 166, 154, 0.35)";
        vpCtx.fillRect(xStart, yTop, buyWidth, barHeight);
        vpCtx.strokeStyle = "rgba(38, 166, 154, 0.6)";
        vpCtx.lineWidth = 0.5;
        vpCtx.strokeRect(xStart, yTop, buyWidth, barHeight);
      }

      // Sell volume (red bar from right edge of buy)
      const sellWidth = (bin.sell_volume / maxVol) * profileWidth * 0.9;
      if (sellWidth > 0.5) {
        vpCtx.fillStyle = "rgba(239, 83, 80, 0.35)";
        vpCtx.fillRect(xStart + buyWidth, yTop, sellWidth, barHeight);
        vpCtx.strokeStyle = "rgba(239, 83, 80, 0.6)";
        vpCtx.lineWidth = 0.5;
        vpCtx.strokeRect(xStart + buyWidth, yTop, sellWidth, barHeight);
      }
    }

    // Draw POC (Point of Control) line — highest volume bin
    const pocBin = profile.bins.reduce((a, b) => (a.total_volume > b.total_volume ? a : b), profile.bins[0]);
    if (pocBin && pocBin.total_volume > 0) {
      const pocPrice = (pocBin.price_low + pocBin.price_high) / 2;
      const pocMcap = toMarketCapValue(pocPrice);
      const pocY = candleSeries.priceToCoordinate(pocMcap);
      if (pocY !== null) {
        vpCtx.strokeStyle = "rgba(255, 215, 64, 0.7)";
        vpCtx.lineWidth = 1;
        vpCtx.setLineDash([4, 4]);
        vpCtx.beginPath();
        vpCtx.moveTo(xStart, pocY);
        vpCtx.lineTo(xStart + profileWidth, pocY);
        vpCtx.stroke();
        vpCtx.setLineDash([]);
      }
    }
  }
}

/* ── Sub-indicator panes (ROC + ATR) — rendered via canvas ───────────── */

let subCanvas = null;
let subCtx = null;
let rocHistory = [];
let atrHistory = [];

function initSubPaneCanvas() {
  const wrapper = $("sub-pane");
  if (!wrapper) return;
  const existing = wrapper.querySelector(".sub-canvas");
  if (existing) existing.remove();

  subCanvas = document.createElement("canvas");
  subCanvas.className = "sub-canvas";
  subCanvas.style.cssText = "width:100%;height:100%;display:block;";
  wrapper.appendChild(subCanvas);
  subCtx = subCanvas.getContext("2d");

  function resizeSub() {
    const dpr = window.devicePixelRatio || 1;
    subCanvas.width = wrapper.clientWidth * dpr;
    subCanvas.height = wrapper.clientHeight * dpr;
    subCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    renderSubPanes();
  }

  const ro = new ResizeObserver(resizeSub);
  ro.observe(wrapper);
  setTimeout(resizeSub, 50);
}

function renderSubPanes() {
  if (!subCtx || !subCanvas) return;

  const dpr = window.devicePixelRatio || 1;
  const w = subCanvas.width / dpr;
  const h = subCanvas.height / dpr;
  subCtx.clearRect(0, 0, w, h);

  if (rocHistory.length < 2) return;

  const totalH = h;
  const rocH = totalH * 0.5;
  const atrH = totalH * 0.5;
  const padding = 4;

  // ── ROC pane ──
  drawSubLine(subCtx, rocHistory, 0, rocH, w, ROC_COLOR, "ROC 3", padding);
  // Zero line for ROC
  const rocMin = Math.min(...rocHistory.map(r => r.value));
  const rocMax = Math.max(...rocHistory.map(r => r.value));
  if (rocMin < 0 && rocMax > 0) {
    const rocRange = rocMax - rocMin || 1;
    const zeroY = padding + (rocH - 2 * padding) * (1 - (0 - rocMin) / rocRange);
    subCtx.strokeStyle = "#5a607166";
    subCtx.lineWidth = 1;
    subCtx.setLineDash([4, 4]);
    subCtx.beginPath();
    subCtx.moveTo(0, zeroY);
    subCtx.lineTo(w, zeroY);
    subCtx.stroke();
    subCtx.setLineDash([]);
  }

  // ── ATR pane ──
  drawSubLine(subCtx, atrHistory, rocH, atrH, w, ATR_COLOR, "ATR 7", padding);

  // Divider
  subCtx.strokeStyle = "#1e2330";
  subCtx.lineWidth = 1;
  subCtx.beginPath();
  subCtx.moveTo(0, rocH);
  subCtx.lineTo(w, rocH);
  subCtx.stroke();
}

function drawSubLine(ctx, data, yOffset, height, width, color, label, padding = 4) {
  if (data.length < 2) return;

  const values = data.map(d => d.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;

  const drawH = height - 2 * padding;
  const stepX = width / (data.length - 1);

  // Label
  ctx.fillStyle = color;
  ctx.font = "bold 10px 'JetBrains Mono', monospace";
  ctx.fillText(label, 6, yOffset + 14);

  // Current value
  const lastVal = values[values.length - 1];
  ctx.fillStyle = "#d1d5e0";
  ctx.font = "10px 'JetBrains Mono', monospace";
  ctx.fillText(lastVal.toFixed(3), 6 + ctx.measureText(label + " ").width + 6, yOffset + 14);

  // Line
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let i = 0; i < data.length; i++) {
    const x = i * stepX;
    const y = yOffset + padding + drawH * (1 - (values[i] - min) / range);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Fill gradient below line
  ctx.lineTo((data.length - 1) * stepX, yOffset + height);
  ctx.lineTo(0, yOffset + height);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, yOffset, 0, yOffset + height);
  grad.addColorStop(0, color + "30");
  grad.addColorStop(1, color + "05");
  ctx.fillStyle = grad;
  ctx.fill();
}

/* ── Regime bar rendering ────────────────────────────────────────────── */

let regimeCanvas = null;
let regimeCtx = null;
let regimeHistory = [];

function initRegimeCanvas() {
  const wrapper = $("regime-bar-canvas");
  if (!wrapper) return;
  const existing = wrapper.querySelector(".regime-canvas");
  if (existing) existing.remove();

  regimeCanvas = document.createElement("canvas");
  regimeCanvas.className = "regime-canvas";
  regimeCanvas.style.cssText = "width:100%;height:100%;display:block;";
  wrapper.appendChild(regimeCanvas);
  regimeCtx = regimeCanvas.getContext("2d");

  function resizeRegime() {
    const dpr = window.devicePixelRatio || 1;
    regimeCanvas.width = wrapper.clientWidth * dpr;
    regimeCanvas.height = wrapper.clientHeight * dpr;
    regimeCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    renderRegimeBar();
  }

  const ro = new ResizeObserver(resizeRegime);
  ro.observe(wrapper);
  setTimeout(resizeRegime, 50);
}

function renderRegimeBar() {
  if (!regimeCtx || !regimeCanvas || !regimeHistory.length) return;

  const dpr = window.devicePixelRatio || 1;
  const w = regimeCanvas.width / dpr;
  const h = regimeCanvas.height / dpr;
  regimeCtx.clearRect(0, 0, w, h);

  const stepX = w / regimeHistory.length;

  for (let i = 0; i < regimeHistory.length; i++) {
    const regime = regimeHistory[i];
    const color = REGIME_COLORS[regime] || "#5a6071";
    regimeCtx.fillStyle = color + "aa";
    regimeCtx.fillRect(i * stepX, 0, stepX + 0.5, h);
  }
}

/* ── Buy/Sell markers on price chart ─────────────────────────────────── */

function updateMarkers() {
  if (!candleSeries) return;
  try {
    candleSeries.setMarkers(markers.sort((a, b) => a.time - b.time));
  } catch (e) {
    // Markers may fail if times are not monotonic
  }
}

let lastBuyMcapPrice = null;

function addSignalMarker(time, signal, signalLabel, regimeLabel, mcapPrice, closedTrade) {
  if (signal === "buy") {
    lastBuyMcapPrice = mcapPrice;
    const priceLabel = mcapPrice ? ` @ ${formatMcap(mcapPrice)}` : "";
    let prefix = signalLabel ? signalLabel.replace("_", " ").toUpperCase() : "BUY";
    markers.push({
      time,
      position: "belowBar",
      color: "#26a69a",
      shape: "arrowUp",
      text: `${prefix}${priceLabel}`,
    });
  } else if (signal === "exit") {
    const priceLabel = mcapPrice ? ` @ ${formatMcap(mcapPrice)}` : "";
    let pnlLabel = "";
    let finalColor = "#ef5350";

    // Give precedence to visual marker difference so the math checks out seamlessly
    if (lastBuyMcapPrice && mcapPrice) {
      const pnl = ((mcapPrice - lastBuyMcapPrice) / lastBuyMcapPrice) * 100;
      const sign = pnl >= 0 ? "+" : "";
      pnlLabel = ` (${sign}${pnl.toFixed(2)}%)`;
      finalColor = pnl >= 0 ? "#26a69a" : "#ef5350";
    } else if (closedTrade && typeof closedTrade.pnl_pct === "number") {
      const sign = closedTrade.pnl_pct >= 0 ? "+" : "";
      pnlLabel = ` (${sign}${closedTrade.pnl_pct.toFixed(2)}%)`;
      finalColor = closedTrade.pnl_pct >= 0 ? "#26a69a" : "#ef5350";
    }

    let prefix = signalLabel ? signalLabel.replace("_", " ").toUpperCase() : "EXIT";
    markers.push({
      time,
      position: "aboveBar",
      color: finalColor,
      shape: "circle",
      text: `${prefix}${priceLabel}${pnlLabel}`,
    });
    // Reset tracker
    lastBuyMcapPrice = null;
  }
}

/**
 * Once chartBaseMcap is known, convert any markers that were placed during
 * the historical pass (when market cap wasn't available yet) and set them
 * with proper price labels.
 */
function flushPendingMarkers() {
  if (!pendingMarkerData.length || !chartBaseMcap) return;
  markers = [];  // rebuild from scratch with correct prices
  lastBuyMcapPrice = null; // reset before replaying
  for (const m of pendingMarkerData) {
    const mcapPrice = toMarketCapValue(m.rawPrice);
    addSignalMarker(m.time, m.signal, m.signalLabel, m.regime, mcapPrice, m.closedTrade);
  }
  pendingMarkerData = [];
  updateMarkers();
}

/* ── Process strategy results from historical data ───────────────────── */

function processHistoricalStrategy(results, candles) {
  if (!results || !results.length) return;

  strategyResults = results;
  markers = [];
  rocHistory = [];
  atrHistory = [];
  regimeHistory = [];

  const emaFastData = [];
  const emaSlowData = [];

  for (let i = 0; i < results.length; i++) {
    const r = results[i];
    const candle = candles[i];
    if (!r || !candle) continue;

    // Collect indicator data
    if (r.indicators) {
      if (r.indicators.ema_fast) {
        emaFastData.push({ time: candle.time, value: toMarketCapValue(r.indicators.ema_fast) });
      }
      if (r.indicators.ema_slow) {
        emaSlowData.push({ time: candle.time, value: toMarketCapValue(r.indicators.ema_slow) });
      }
      if (r.indicators.roc !== null && r.indicators.roc !== undefined) {
        rocHistory.push({ time: candle.time, value: r.indicators.roc });
      }
      if (r.indicators.atr !== null && r.indicators.atr !== undefined) {
        atrHistory.push({ time: candle.time, value: r.indicators.atr });
      }
    }

    // Regime history
    regimeHistory.push(r.regime || "idle");

    // Markers — execution is at the OPEN of the candle (1-bar delay model).
    // Use candle.open for the label price so it sits within the visible candle range.
    // entry_price/exit_price include slippage+delay and would appear outside the candle.
    if (r.forward_test && (r.forward_test.trade_action === "buy" || r.forward_test.trade_action === "exit")) {
      const ft = r.forward_test;
      const signal = ft.trade_action;
      const signalLabel = ft.trade_label;
      const closedTrade = ft.closed_trade;
      // Always use candle open for visual label — the fill price (entry/exit_price)
      // includes delay + slippage and would render outside the candle's range.
      const rawExecPrice = candle?.open ?? candle?.close;

      if (chartBaseMcap) {
        const mcapExecPrice = toMarketCapValue(rawExecPrice);
        addSignalMarker(candle.time, signal, signalLabel, r.regime, mcapExecPrice, closedTrade);
      } else {
        pendingMarkerData.push({
          time: candle.time,
          signal,
          signalLabel,
          regime: r.regime,
          rawPrice: rawExecPrice,
          closedTrade,
        });
      }
    }
  }

  // Set EMA data on chart
  if (emaFastData.length) emaFastSeries.setData(emaFastData);
  if (emaSlowData.length) emaSlowSeries.setData(emaSlowData);

  // Update markers
  updateMarkers();

  // Render sub panes
  renderSubPanes();
  renderRegimeBar();
  renderVolumeProfiles();

  // Update strategy panel with last result
  updateStrategyPanel(results[results.length - 1]);
}

/* ── Process live strategy result ────────────────────────────────────── */

function processLiveStrategy(result, candle, mcapCandle) {
  if (!result) return;

  // On the first live tick, chartBaseMcap is now known — flush any historical
  // markers that were waiting for market cap conversion.
  flushPendingMarkers();

  strategyResults.push(result);

  // Update indicators
  if (result.indicators) {
    if (result.indicators.ema_fast && candle) {
      emaFastSeries.update({ time: candle.time, value: toMarketCapValue(result.indicators.ema_fast) });
    }
    if (result.indicators.ema_slow && candle) {
      emaSlowSeries.update({ time: candle.time, value: toMarketCapValue(result.indicators.ema_slow) });
    }
    if (result.indicators.roc !== null && result.indicators.roc !== undefined) {
      rocHistory.push({ time: candle?.time, value: result.indicators.roc });
      if (rocHistory.length > 300) rocHistory.shift();
    }
    if (result.indicators.atr !== null && result.indicators.atr !== undefined) {
      atrHistory.push({ time: candle?.time, value: result.indicators.atr });
      if (atrHistory.length > 300) atrHistory.shift();
    }
  }

  regimeHistory.push(result.regime || "idle");
  if (regimeHistory.length > 300) regimeHistory.shift();

  // Use candle open for the visual label price — entry/exit_price include
  // delay + slippage and would render outside the candle's visible range.
  if (result.forward_test && mcapCandle) {
    const ft = result.forward_test;
    if (ft.trade_action === "buy" || ft.trade_action === "exit") {
      // candle.open is the reference price that sits within the candle range.
      const mcapExecPrice = mcapCandle.open;

      addSignalMarker(
        mcapCandle.time,
        ft.trade_action,
        ft.trade_label,
        result.regime,
        mcapExecPrice,
        ft.closed_trade
      );
      updateMarkers();
    }
  }

  // Re-render overlays
  renderSubPanes();
  renderRegimeBar();
  renderVolumeProfiles();

  // Update panel
  updateStrategyPanel(result);
}

/* ── WebSocket connection ────────────────────────────────────────────── */

function connect(mint, timeframe) {
  if (ws) { ws.onclose = null; ws.close(); }
  clearTimeout(reconnectTimer);
  currentMint = mint; currentTf = timeframe;
  tfSeconds = timeframeToSeconds(timeframe);
  openPrice = null; lastClose = null;
  initMcapSol = 0; initMcapUsd = 0;
  chartBasePrice = null;
  chartBaseMcap = null;
  chartCurrency = "USD";
  currentPrecision = 6;
  lastCandleTime = null;
  lastCandleClose = null;
  liveMcapCandle = null;
  tokenBar.classList.add("hidden");
  tradeFeed.innerHTML = "";
  strategyResults = [];
  markers = [];
  lastBuyMcapPrice = null;
  pendingMarkerData = [];
  rocHistory = [];
  atrHistory = [];
  regimeHistory = [];
  if (candleSeries) { candleSeries.setData([]); }
  if (volSeries) volSeries.setData([]);
  if (emaFastSeries) emaFastSeries.setData([]);
  if (emaSlowSeries) emaSlowSeries.setData([]);
  showOverlay("⏳", "Connecting…");
  setDot("connecting", "Connecting…");

  const paramsStr = encodeURIComponent(JSON.stringify(engineParams));
  ws = new WebSocket(`${WS_BASE}/${mint}?timeframe=${timeframe}&params=${paramsStr}`);

  ws.onopen = () => {
    reconnectMs = RECONNECT_MS;
    setDot("connected", "Live");
    showOverlay("📡", "Waiting for data…");
  };

  ws.onmessage = ev => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.type === "token_info") {
      const d = msg.data;
      tokenName.textContent = d.name || mint.slice(0, 8) + "…";
      tokenSymbol.textContent = d.symbol ? `$${d.symbol}` : "";
      if (d.description) tokenName.title = d.description;

      if (d.image_uri || d.imageUri) {
        tokenLogo.src = d.image_uri || d.imageUri;
        tokenLogo.style.display = "block";
        tokenLogo.onerror = () => { tokenLogo.style.display = "none"; };
      }

      if (d.usd_market_cap && d.usd_market_cap > 0) {
        initMcapUsd = Number(d.usd_market_cap);
        mcapEl.textContent = "$" + fmtLarge(initMcapUsd);
      }
      if (d.virtual_sol_reserves) {
        initMcapSol = Number(d.virtual_sol_reserves) / 1e9;
      }
      if (!chartBaseMcap) {
        if (initMcapUsd > 0) { chartBaseMcap = initMcapUsd; chartCurrency = "USD"; }
        else if (initMcapSol > 0) { chartBaseMcap = initMcapSol; chartCurrency = "SOL"; }
      }

      // Social links
      tokenBar.querySelectorAll(".social-link").forEach(el => el.remove());
      const socials = [
        { key: "twitter", label: "𝕏", base: "https://twitter.com/" },
        { key: "telegram", label: "TG", base: "" },
        { key: "website", label: "🌐", base: "" },
      ];
      socials.forEach(({ key, label, base }) => {
        let href = d[key];
        if (!href) return;
        if (base && !href.startsWith("http")) href = base + href.replace(/^@/, "");
        const a = document.createElement("a");
        a.className = "social-link";
        a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.textContent = label;
        tokenBar.appendChild(a);
      });

      tokenBar.classList.remove("hidden");
    }

    else if (msg.type === "historical") {
      const cs = msg.candles;
      if (!cs || !cs.length) { showOverlay("🔍", "No historical data. Waiting for live trades…"); return; }
      if (LIVE_ONLY_MARKETCAP) {
        if (cs[cs.length - 1]?.close > 0) chartBasePrice = cs[cs.length - 1].close;
        showOverlay("📡", "Live market-cap mode. Waiting for ticks…");

        // Still process strategy data even in live marketcap mode
        if (msg.strategy) {
          processHistoricalStrategy(msg.strategy, cs);
        }
        return;
      }
      if (cs[cs.length - 1]?.close > 0) chartBasePrice = cs[cs.length - 1].close;
      ensureMcapBase(cs[0].open);
      const mcapCandles = cs.map(c => candleToMcap(c));
      const colored = [];
      for (let i = 0; i < mcapCandles.length; i++) {
        const prev = i > 0 ? mcapCandles[i - 1].close : null;
        colored.push(withCandleColors(mcapCandles[i], prev));
      }
      syncPriceScale(mcapCandles[mcapCandles.length - 1].close);
      candleSeries.setData(colored);
      volSeries.setData(cs.map(c => ({ time: c.time, value: c.volume, color: c.close >= c.open ? "#26a69a33" : "#ef535033" })));
      openPrice = mcapCandles[0].open;
      lastCandleTime = mcapCandles[mcapCandles.length - 1].time;
      lastCandleClose = mcapCandles[mcapCandles.length - 1].close;
      updateOhlc(mcapCandles[mcapCandles.length - 1]);
      updatePrice(mcapCandles[mcapCandles.length - 1].close);

      // Process strategy results
      if (msg.strategy) {
        processHistoricalStrategy(msg.strategy, cs);
      }

      chart.timeScale().scrollToRealTime();
      chart.timeScale().fitContent();
      hideOverlay();
    }

    else if (msg.type === "candle") {
      let closeMcap = 0;
      if (msg.market_cap_usd > 0) {
        chartCurrency = "USD";
        closeMcap = Number(msg.market_cap_usd);
        mcapEl.textContent = "$" + fmtLarge(closeMcap);
      } else if (msg.market_cap_sol > 0) {
        chartCurrency = "SOL";
        closeMcap = Number(msg.market_cap_sol);
        mcapEl.textContent = closeMcap.toFixed(1) + " SOL";
      }

      let c;
      if (closeMcap > 0 && msg.candle?.time) {
        c = buildMcapCandleFromTick(msg.candle.time, closeMcap, msg.candle?.volume || 0, !!msg.is_new);
      } else {
        if (closeMcap === 0 && chartBaseMcap && chartBasePrice) {
          c = candleToMcap(msg.candle);
        } else {
          c = candleToMcapFromCloseAnchor(msg.candle, chartBaseMcap || 1);
        }
      }
      if (!c) return;
      syncPriceScale(c.close);
      pushCandleWithContinuity(c);
      if ((msg.candle?.volume || 0) > 0) {
        volSeries.update({ time: msg.candle.time, value: msg.candle.volume, color: msg.candle.close >= msg.candle.open ? "#26a69a33" : "#ef535033" });
      }
      updatePrice(c.close);
      if (chartCurrency === "USD") {
        mcapEl.textContent = "$" + fmtLarge(c.close);
      } else {
        mcapEl.textContent = c.close.toFixed(1) + " SOL";
      }
      if (openPrice === null) openPrice = c.open;

      if (msg.trade) {
        addTrade(msg.trade);
        if (volEl) {
          const prev = parseFloat(volEl.textContent) || 0;
          volEl.textContent = (prev + msg.trade.sol_amount).toFixed(2) + " SOL";
        }
      }

      // Process live strategy
      if (msg.strategy) {
        processLiveStrategy(msg.strategy, msg.candle, c);
      }

      chart.timeScale().scrollToRealTime();
      hideOverlay();
    }

    else if (msg.type === "error") showOverlay("⚠️", msg.message || "Error");
    else if (msg.type === "ping") ws.send(JSON.stringify({ type: "pong" }));
  };

  ws.onerror = () => { setDot("error", "Error"); showOverlay("❌", "Connection error. Reconnecting…"); };
  ws.onclose = () => {
    setDot("error", "Disconnected");
    showOverlay("🔌", `Disconnected — reconnecting in ${reconnectMs / 1000}s…`);
    reconnectTimer = setTimeout(() => {
      reconnectMs = Math.min(reconnectMs * 2, 30000);
      connect(currentMint, currentTf);
    }, reconnectMs);
  };
}

/* ── Event listeners ─────────────────────────────────────────────────── */

function loadToken() {
  const mint = mintInput.value.trim();
  if (!mint) return;
  if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint)) { showOverlay("⚠️", "Invalid Solana address"); return; }
  connect(mint, currentTf);
}

loadBtn.addEventListener("click", loadToken);
mintInput.addEventListener("keydown", e => { if (e.key === "Enter") loadToken(); });
tfBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    if (btn.dataset.tf === currentTf) return;
    tfBtns.forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentTf = btn.dataset.tf;
    if (currentMint) connect(currentMint, currentTf);
  });
});

/* ── Settings Logic ──────────────────────────────────────────────────── */

function renderSettings() {
  settingsForm.innerHTML = "";
  for (const [key, val] of Object.entries(engineParams)) {
    const group = document.createElement("div");
    group.className = "param-group";
    const label = document.createElement("label");
    label.className = "param-label";
    label.textContent = key;
    const input = document.createElement("input");
    input.className = "param-input";
    input.dataset.key = key;
    input.value = val;
    // determine type
    if (Number.isInteger(val)) input.type = "number";
    else { input.type = "number"; input.step = "0.01"; }

    group.append(label, input);
    settingsForm.append(group);
  }
}

settingsBtn.addEventListener("click", () => {
  renderSettings();
  settingsModal.classList.remove("hidden");
});

closeSettingsBtn.addEventListener("click", () => {
  settingsModal.classList.add("hidden");
});

settingsModal.addEventListener("click", (e) => {
  if (e.target === settingsModal) settingsModal.classList.add("hidden");
});

applySettingsBtn.addEventListener("click", () => {
  const inputs = settingsForm.querySelectorAll(".param-input");
  inputs.forEach(inp => {
    const key = inp.dataset.key;
    const isInt = Number.isInteger(engineParams[key]);
    engineParams[key] = isInt ? parseInt(inp.value, 10) : parseFloat(inp.value);
  });
  settingsModal.classList.add("hidden");
  if (currentMint) connect(currentMint, currentTf);
});

/* Re-render volume profiles when chart view changes */
function setupChartRedraw() {
  if (!chart) return;
  chart.timeScale().subscribeVisibleTimeRangeChange(() => {
    renderVolumeProfiles();
  });
  chart.subscribeCrosshairMove(() => {
    renderVolumeProfiles();
  });
}

initChart();
initVolumeProfileCanvas();
initSubPaneCanvas();
initRegimeCanvas();
setupChartRedraw();

/* ════════════════════════════════════════════════════════════════════════
   NEW PAGES: Navigation + Recorder + Viewer + Backtest
   ════════════════════════════════════════════════════════════════════════ */

const API_BASE = `${location.protocol}//${location.host}`;

/* ── Page Navigation ─────────────────────────────────────────────────── */

const navTabs = document.querySelectorAll(".nav-tab");
const pages = document.querySelectorAll(".page");

function switchPage(pageId) {
  pages.forEach(p => p.classList.remove("active"));
  navTabs.forEach(t => t.classList.remove("active"));
  const target = document.getElementById(`page-${pageId}`);
  const tab = document.querySelector(`.nav-tab[data-page="${pageId}"]`);
  if (target) target.classList.add("active");
  if (tab) tab.classList.add("active");

  // Refresh data when switching to pages
  if (pageId === "recorder") { loadRecordingsList("recordings-list"); checkRecorderStatus(); }
  if (pageId === "viewer") loadRecordingsList("viewer-recordings-list", true);
  if (pageId === "backtest") { loadBacktestsList(); loadRecordingsDropdown(); }
}

navTabs.forEach(tab => tab.addEventListener("click", () => switchPage(tab.dataset.page)));

/* ── Shared helpers ──────────────────────────────────────────────────── */

async function apiFetch(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, { headers: { "Content-Type": "application/json" }, ...opts });
  return res.json();
}

function fmtTs(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function fmtDuration(start, end) {
  if (!start || !end) return "—";
  const s = Math.round(end - start);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  return (s / 3600).toFixed(1) + "h";
}

/* ── Recording card HTML ─────────────────────────────────────────────── */

function renderRecordingCard(rec, opts = {}) {
  const statusClass = rec.status === "recording" ? "status-recording" : "status-completed";
  const actions = opts.viewerMode
    ? `<button class="btn btn-primary btn-sm" onclick="loadViewer(${rec.id})">📊 View Chart</button>
       <button class="btn btn-danger btn-xs" onclick="deleteRecording(${rec.id}, event)">🗑</button>`
    : `<button class="btn btn-danger btn-xs" onclick="deleteRecording(${rec.id}, event)">🗑</button>`;
  return `
    <div class="recording-card" data-id="${rec.id}">
      <div class="rec-card-header">
        <div><span class="rec-card-name">${rec.token_name || 'Unknown'}</span> <span class="rec-card-symbol">${rec.token_symbol ? '$' + rec.token_symbol : ''}</span></div>
        <div class="rec-card-badges">
          <span class="rec-card-badge">${rec.timeframe}</span>
          <span class="rec-card-badge ${statusClass}">${rec.status}</span>
        </div>
      </div>
      <div class="rec-card-details">
        <span>🕐 ${fmtTs(rec.started_at)}</span>
        <span>📊 ${rec.candle_count} candles</span>
        ${rec.stopped_at ? `<span>⏱ ${fmtDuration(rec.started_at, rec.stopped_at)}</span>` : ''}
      </div>
      <div class="rec-card-mint">${rec.mint || ''}</div>
      <div class="rec-card-actions">${actions}</div>
    </div>`;
}

/* ── Recordings list ─────────────────────────────────────────────────── */

async function loadRecordingsList(containerId, viewerMode = false) {
  const list = await apiFetch("/api/recordings");
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!list.length) {
    el.innerHTML = `<div class="empty-state">No recordings yet.</div>`;
    return;
  }
  el.innerHTML = list.map(r => renderRecordingCard(r, { viewerMode })).join("");
}

async function deleteRecording(id, e) {
  if (e) e.stopPropagation();
  if (!confirm("Delete this recording?")) return;
  await apiFetch(`/api/recordings/${id}`, { method: "DELETE" });
  loadRecordingsList("recordings-list");
  loadRecordingsList("viewer-recordings-list", true);
}

/* ── Recorder ────────────────────────────────────────────────────────── */

let recPollTimer = null;

async function checkRecorderStatus() {
  const data = await apiFetch("/api/recorder/status");
  const statusEl = document.getElementById("rec-status");
  const startBtn = document.getElementById("rec-start-btn");
  const stopBtn = document.getElementById("rec-stop-btn");
  if (data.active) {
    statusEl.classList.remove("hidden");
    startBtn.classList.add("hidden");
    stopBtn.classList.remove("hidden");
    document.getElementById("rec-status-mint").textContent = (data.token_name || data.mint?.slice(0, 8)) + (data.token_symbol ? ` $${data.token_symbol}` : "");
    document.getElementById("rec-status-tf").textContent = data.timeframe;
    document.getElementById("rec-status-candles").textContent = `${data.candle_count} candles`;
    if (!recPollTimer) recPollTimer = setInterval(checkRecorderStatus, 3000);
  } else {
    statusEl.classList.add("hidden");
    startBtn.classList.remove("hidden");
    stopBtn.classList.add("hidden");
    if (recPollTimer) { clearInterval(recPollTimer); recPollTimer = null; }
  }
}

document.getElementById("rec-start-btn").addEventListener("click", async () => {
  const mint = document.getElementById("rec-mint-input").value.trim();
  if (!mint) return alert("Enter a token address");
  const tf = document.getElementById("rec-tf-select").value;
  const data = await apiFetch("/api/recorder/start", { method: "POST", body: JSON.stringify({ mint, timeframe: tf }) });
  if (data.error) return alert(data.error);
  checkRecorderStatus();
});

document.getElementById("rec-stop-btn").addEventListener("click", async () => {
  await apiFetch("/api/recorder/stop", { method: "POST" });
  checkRecorderStatus();
  loadRecordingsList("recordings-list");
});

/* ── Offline Chart Formatter ─────────── */

async function formatOfflineCandles(mint, rawCandles, timeframeStr) {
  if (!rawCandles || !rawCandles.length) return { candles: [], currency: "SOL" };

  let basePrice = rawCandles[0].open;
  let baseMcap = 0;
  let ccy = "SOL";

  try {
    const tInfo = await apiFetch(`/api/token/${mint}`);
    if (tInfo) {
      // Determine authentic SOL price to prevent decoupled market cap evaluations
      let solPrice = 160;
      if (tInfo.price_usd && tInfo.price_sol) {
        const pU = parseFloat(tInfo.price_usd);
        const pS = parseFloat(tInfo.price_sol);
        if (pU > 0 && pS > 0) solPrice = pU / pS;
      } else if (tInfo.usd_market_cap && tInfo.market_cap) {
        const sp = tInfo.usd_market_cap / tInfo.market_cap;
        if (sp > 50 && sp < 1000) solPrice = sp;
      }

      if (tInfo.usd_market_cap || tInfo.price_usd) {
        baseMcap = basePrice * 1_000_000_000 * solPrice;
        ccy = "USD";
      } else {
        baseMcap = basePrice * 1e9;
      }
    } else {
      baseMcap = basePrice * 1e9;
    }
  } catch (e) {
    baseMcap = basePrice * 1e9;
  }

  if (!baseMcap || isNaN(baseMcap) || baseMcap <= 0) baseMcap = basePrice * 1e9;

  const toMcap = (p) => {
    if (!p) return 0;
    return baseMcap * (p / basePrice);
  };

  const tfSec = timeframeToSeconds(timeframeStr);
  const formatted = [];
  let lastTime = null;
  let lastClose = null;

  for (const c of rawCandles) {
    if (lastTime !== null && lastClose !== null && c.time > lastTime + tfSec) {
      const gap = Math.floor((c.time - lastTime) / tfSec) - 1;
      if (gap <= 15) { // Fill up to 15 empty candles to preserve continuity/connections
        for (let t = lastTime + tfSec; t < c.time; t += tfSec) {
          formatted.push({
            time: t,
            open: lastClose, high: lastClose, low: lastClose, close: lastClose,
            volume: 0,
            color: CANDLE_FLAT, borderColor: CANDLE_FLAT, wickColor: CANDLE_FLAT
          });
          lastTime = t;
        }
      }
    }

    let open = toMcap(c.open);
    let high = toMcap(c.high);
    let low = toMcap(c.low);
    const close = toMcap(c.close);

    // Crucial for replicating the live charting: bridge open/close 
    // to strictly form a continuous timeline preventing disjointed dashes
    if (lastClose !== null) {
      open = lastClose;
      high = Math.max(lastClose, high, close);
      low = Math.min(lastClose, low, close);
    }

    let color = CANDLE_FLAT;
    if (close > open) color = CANDLE_UP;
    else if (close < open) color = CANDLE_DOWN;
    else if (lastClose !== null) {
      if (close > lastClose) color = CANDLE_UP;
      else if (close < lastClose) color = CANDLE_DOWN;
    }

    formatted.push({ ...c, time: c.time, open, high, low, close, volume: c.volume || 0, color, borderColor: color, wickColor: color });
    lastTime = c.time;
    lastClose = close;
  }

  return { candles: formatted, currency: ccy };
}

/* ── Viewer ───────────────────────────────────────────────────────────── */

let viewerChart = null;

async function loadViewer(recordingId) {
  const rec = await apiFetch(`/api/recordings/${recordingId}`);
  const candles = await apiFetch(`/api/recordings/${recordingId}/candles`);
  if (!candles.length) return alert("No candles in this recording");

  document.getElementById("viewer-select-area").classList.add("hidden");
  document.getElementById("viewer-chart-area").classList.remove("hidden");
  document.getElementById("viewer-token-name").textContent = rec.token_name || "Unknown";
  document.getElementById("viewer-token-symbol").textContent = rec.token_symbol ? `$${rec.token_symbol}` : "";
  document.getElementById("viewer-meta-tf").textContent = rec.timeframe;
  document.getElementById("viewer-meta-candles").textContent = `${candles.length} candles`;

  const wrapper = document.getElementById("viewer-chart");
  wrapper.innerHTML = "";
  viewerChart = LightweightCharts.createChart(wrapper, {
    layout: { background: { color: "#0d0f12" }, textColor: "#5a6071" },
    grid: { vertLines: { color: "#1e2330" }, horzLines: { color: "#1e2330" } },
    timeScale: { borderColor: "#1e2330", timeVisible: true, secondsVisible: true },
    rightPriceScale: { borderColor: "#1e2330" },
    width: wrapper.clientWidth, height: wrapper.clientHeight,
  });

  const formattedData = await formatOfflineCandles(rec.mint, candles, rec.timeframe);
  chartCurrency = formattedData.currency;

  const cs = viewerChart.addCandlestickSeries({
    upColor: CANDLE_UP, downColor: CANDLE_DOWN,
    borderUpColor: CANDLE_UP, borderDownColor: CANDLE_DOWN,
    wickUpColor: CANDLE_UP, wickDownColor: CANDLE_DOWN,
    priceFormat: { type: 'custom', minMove: 1, formatter: p => formatMcap(p) }
  });
  cs.setData(formattedData.candles);

  const vs = viewerChart.addHistogramSeries({ color: "#5865f222", priceFormat: { type: "volume" }, priceScaleId: "vol" });
  viewerChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  vs.setData(formattedData.candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.close >= c.open ? "#26a69a33" : "#ef535033" })));

  viewerChart.timeScale().fitContent();
  new ResizeObserver(() => viewerChart.applyOptions({ width: wrapper.clientWidth, height: wrapper.clientHeight })).observe(wrapper);
}

document.getElementById("viewer-back-btn").addEventListener("click", () => {
  document.getElementById("viewer-select-area").classList.remove("hidden");
  document.getElementById("viewer-chart-area").classList.add("hidden");
  if (viewerChart) { viewerChart.remove(); viewerChart = null; }
});

/* ── Backtest ─────────────────────────────────────────────────────────── */

let btChart = null;

async function loadRecordingsDropdown() {
  const list = await apiFetch("/api/recordings");
  const sel = document.getElementById("bt-recording-select");
  sel.innerHTML = `<option value="">— Choose a recording —</option>` +
    list.filter(r => r.status === "completed").map(r =>
      `<option value="${r.id}">${r.token_name || r.mint?.slice(0, 8)} ($${r.token_symbol || '?'}) — ${r.timeframe} — ${r.candle_count} candles</option>`
    ).join("");
}

async function loadBacktestsList() {
  const list = await apiFetch("/api/backtests");
  const el = document.getElementById("backtests-list");
  if (!list.length) { el.innerHTML = `<div class="empty-state">No backtests yet.</div>`; return; }
  el.innerHTML = list.map(bt => {
    const pnlClass = bt.total_pnl >= 0 ? "pos" : "neg";
    const pnlSign = bt.total_pnl >= 0 ? "+" : "";
    return `
    <div class="backtest-card" onclick="loadBacktestResult(${bt.id})">
      <div class="bt-card-header">
        <div><span class="bt-card-name">${bt.token_name || bt.mint?.slice(0, 8)}</span> <span class="rec-card-symbol">${bt.token_symbol ? '$' + bt.token_symbol : ''}</span></div>
        <div class="rec-card-badges"><span class="rec-card-badge">${bt.timeframe}</span></div>
      </div>
      <div class="bt-card-stats">
        <div class="bt-stat"><span class="bt-stat-label">Trades</span><span class="bt-stat-value">${bt.total_trades}</span></div>
        <div class="bt-stat"><span class="bt-stat-label">Win Rate</span><span class="bt-stat-value">${bt.win_rate.toFixed(1)}%</span></div>
        <div class="bt-stat"><span class="bt-stat-label">PnL</span><span class="bt-stat-value ${pnlClass}">${pnlSign}${bt.total_pnl.toFixed(4)}</span></div>
      </div>
      <div class="rec-card-details" style="margin-top:8px"><span>🕐 ${fmtTs(bt.created_at)}</span></div>
      <div class="bt-card-actions"><button class="btn btn-danger btn-xs" onclick="deleteBacktest(${bt.id}, event)">🗑</button></div>
    </div>`;
  }).join("");
}

async function deleteBacktest(id, e) {
  if (e) e.stopPropagation();
  if (!confirm("Delete this backtest?")) return;
  await apiFetch(`/api/backtests/${id}`, { method: "DELETE" });
  loadBacktestsList();
}

document.getElementById("bt-run-btn").addEventListener("click", async () => {
  const recId = document.getElementById("bt-recording-select").value;
  if (!recId) return alert("Select a recording first");
  const prog = document.getElementById("bt-progress");
  prog.classList.remove("hidden");
  document.getElementById("bt-run-btn").disabled = true;
  try {
    const result = await apiFetch("/api/backtest", { method: "POST", body: JSON.stringify({ recording_id: parseInt(recId), engine_params: engineParams }) });
    if (result.error) { alert(result.error); return; }
    loadBacktestsList();
    loadBacktestResult(result.backtest_id);
  } finally {
    prog.classList.add("hidden");
    document.getElementById("bt-run-btn").disabled = false;
  }
});

document.getElementById("bt-params-btn").addEventListener("click", () => {
  renderSettings();
  settingsModal.classList.remove("hidden");
});

async function loadBacktestResult(id) {
  const bt = await apiFetch(`/api/backtests/${id}`);
  if (!bt || bt.error) return alert("Failed to load backtest");

  document.getElementById("bt-controls").classList.add("hidden");
  document.querySelector(".backtests-section").classList.add("hidden");
  document.getElementById("bt-result-area").classList.remove("hidden");

  document.getElementById("bt-result-name").textContent = `${bt.token_name || bt.mint?.slice(0, 8)} ${bt.token_symbol ? '$' + bt.token_symbol : ''}`;
  document.getElementById("bt-result-tf").textContent = bt.timeframe;

  // Stats
  const s = bt.summary_json || {};
  const statsEl = document.getElementById("bt-stats-grid");
  const pnlC = s.total_pnl_sol >= 0 ? "pos" : "neg";
  statsEl.innerHTML = [
    { l: "Total Trades", v: s.total_trades || 0 },
    { l: "Win Rate", v: `${(s.win_rate || 0).toFixed(1)}%` },
    { l: "Total PnL", v: `${s.total_pnl_sol >= 0 ? '+' : ''}${(s.total_pnl_sol || 0).toFixed(4)} SOL`, c: pnlC },
    { l: "Final Balance", v: `${(s.current_balance || 1).toFixed(4)} SOL` },
    { l: "Max Drawdown", v: `${(s.max_drawdown_pct || 0).toFixed(2)}%`, c: "neg" },
    { l: "Fees Paid", v: `${(s.total_fees_paid || 0).toFixed(4)} SOL` },
  ].map(x => `<div class="bt-stats-card"><div class="bt-stats-card-label">${x.l}</div><div class="bt-stats-card-value ${x.c || ''}">${x.v}</div></div>`).join("");

  // Chart
  const wrapper = document.getElementById("bt-chart");
  wrapper.innerHTML = "";
  if (btChart) btChart.remove();
  btChart = LightweightCharts.createChart(wrapper, {
    layout: { background: { color: "#0d0f12" }, textColor: "#5a6071" },
    grid: { vertLines: { color: "#1e2330" }, horzLines: { color: "#1e2330" } },
    timeScale: { borderColor: "#1e2330", timeVisible: true, secondsVisible: true },
    rightPriceScale: { borderColor: "#1e2330" },
    width: wrapper.clientWidth, height: wrapper.clientHeight,
  });

  const candles = bt.candles || [];
  const formattedData = await formatOfflineCandles(bt.mint, candles, bt.timeframe);
  chartCurrency = formattedData.currency;

  const cs2 = btChart.addCandlestickSeries({
    upColor: CANDLE_UP, downColor: CANDLE_DOWN,
    borderUpColor: CANDLE_UP, borderDownColor: CANDLE_DOWN,
    wickUpColor: CANDLE_UP, wickDownColor: CANDLE_DOWN,
    priceFormat: { type: 'custom', minMove: 1, formatter: p => formatMcap(p) }
  });
  cs2.setData(formattedData.candles);

  const vs = btChart.addHistogramSeries({ color: "#5865f222", priceFormat: { type: "volume" }, priceScaleId: "vol" });
  btChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  vs.setData(formattedData.candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.close >= c.open ? "#26a69a33" : "#ef535033" })));

  // Markers
  const btMarkers = [];
  for (const c of formattedData.candles) {
    if (c.trade_action === "buy") {
      btMarkers.push({ time: c.time, position: "belowBar", color: CANDLE_UP, shape: "arrowUp", text: `BUY @ ${formatMcap(c.open)}` });
    } else if (c.trade_action === "exit") {
      btMarkers.push({ time: c.time, position: "aboveBar", color: CANDLE_DOWN, shape: "circle", text: `EXIT @ ${formatMcap(c.open)}` });
    }
  }
  if (btMarkers.length) cs2.setMarkers(btMarkers.sort((a, b) => a.time - b.time));

  btChart.timeScale().fitContent();
  new ResizeObserver(() => btChart.applyOptions({ width: wrapper.clientWidth, height: wrapper.clientHeight })).observe(wrapper);

  // Trades table
  const tbody = document.getElementById("bt-trades-tbody");
  const trades = bt.trades || [];
  tbody.innerHTML = trades.map((t, i) => {
    const pnlClass = t.pnl_sol >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
    return `<tr>
      <td>${i + 1}</td>
      <td>${fmtTs(t.entry_time)}</td>
      <td>${t.entry_price?.toExponential(4) || '—'}</td>
      <td>${fmtTs(t.exit_time)}</td>
      <td>${t.exit_price?.toExponential(4) || '—'}</td>
      <td class="${pnlClass}">${t.pnl_sol >= 0 ? '+' : ''}${t.pnl_sol?.toFixed(6) || '0'}</td>
      <td class="${pnlClass}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct?.toFixed(2) || '0'}%</td>
      <td>${t.entry_reason || '—'}</td>
      <td>${t.exit_reason || '—'}</td>
    </tr>`;
  }).join("");
}

document.getElementById("bt-result-back-btn").addEventListener("click", () => {
  document.getElementById("bt-controls").classList.remove("hidden");
  document.querySelector(".backtests-section").classList.remove("hidden");
  document.getElementById("bt-result-area").classList.add("hidden");
  if (btChart) { btChart.remove(); btChart = null; }
});

// Make functions globally available for onclick handlers
window.loadViewer = loadViewer;
window.deleteRecording = deleteRecording;
window.loadBacktestResult = loadBacktestResult;
window.deleteBacktest = deleteBacktest;
