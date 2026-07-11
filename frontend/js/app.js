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
const EMA_MACRO_COLOR = "#c084fc"; // Purple — macro trend gate
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
let emaFastSeries, emaSlowSeries, emaMacroSeries;  // EMA overlay lines
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

/* Strategy Engine Parameters — V1 (Physics-based regime detection) */
let engineParamsV1 = {
  ema_fast: 3, ema_slow: 7, atr_period: 7, roc_period: 3, warmup: 30,
  signal_strong: 4, signal_weak: 2, signal_noise: 1.1535714285714287,
  exhaustion_bars_limit: 3, delta_threshold: 0.3, kalman_gamma: 0.125,
  min_trend_bars: 2, reversal_confirm_bars: 2, chop_atr_pct: 0.3,
  chop_spread_pct: 0.05, reversal_exit_confirm_bars: 0,
  s_effective_threshold: 0.5, exhaustion_persist_bars: 6,
  regime_lookback: 6, persistence_threshold: 2, momentum_mean_threshold: 0.0,
  ema_min_spread_pct: 0.02, confidence_high: 0.79, confidence_low: 0.19,
  entry_confidence_high: 0.79, entry_confidence_low: 0.19,
  confidence_w1: 0.3, confidence_w2: 0.25, confidence_w3: 0.25, confidence_w4: 0.2,
  atr_floor_k: 0, ema_cross_persist_bars: 2, exhaustion_s_decay_bars: 1,
  local_range_bars: 80, local_range_threshold_pct: 10, sign_flip_threshold: 0,
  stability_bars: 5,
  spike_atr_multiplier: 1.2,
  spike_lookback_bars: 9,
  exhaustion_stall_bars: 6,
  exhaustion_stall_atr_pct: 3,
  body_baseline_bars: 160,
  overextension_k: 0.08,
  momentum_peak_bars: 1,
  consolidation_range_pct: 25,
  confidence_very_high: 0.86,
  ema_macro_period: 7,
  stoploss_pct: 0,
  takeprofit_pct: 0,
  // Confidence-scaled TP/SL (0 = use static value above)
  takeprofit_pct_low: 0,
  takeprofit_pct_high: 0,
  stoploss_pct_low: 0,
  stoploss_pct_high: 0,
};

/* Engine version: 1 = V1 (Physics) */
let engineVersion = 1;

/* Active params getter — returns the params for the current engine version */
function getEngineParams() {
  return engineParamsV1;
}
/* Legacy compat — direct references to `engineParams` throughout the file */
let engineParams = engineParamsV1;

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

  emaMacroSeries = chart.addLineSeries({
    color: EMA_MACRO_COLOR,
    lineWidth: 1,
    lineStyle: 1,  // dashed
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
  const emaMacroData = [];

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
      if (r.indicators.ema_macro) {
        emaMacroData.push({ time: candle.time, value: toMarketCapValue(r.indicators.ema_macro) });
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
      // Always use the real exact calculated price if available (with delay and slippage);
      // fallback to candle open if not available.
      const rawExecPrice = ft.opened_trade?.entry_price || ft.closed_trade?.exit_price || (candle?.open ?? candle?.close);

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
  if (emaMacroData.length) emaMacroSeries.setData(emaMacroData);

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
    if (result.indicators.ema_macro && candle) {
      emaMacroSeries.update({ time: candle.time, value: toMarketCapValue(result.indicators.ema_macro) });
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

  if (result.forward_test && mcapCandle && candle) {
    const ft = result.forward_test;
    if (ft.trade_action === "buy" || ft.trade_action === "exit") {
      const rawExecPrice = ft.opened_trade?.entry_price || ft.closed_trade?.exit_price || candle.open;
      const mcapExecPrice = (ft.opened_trade || ft.closed_trade) ? toMarketCapValue(rawExecPrice) : mcapCandle.open;

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
  const testerConfig = {
    buy_size_sol: parseFloat(document.getElementById("tester-buy-size").value) || 0.1,
    slippage_pct: parseFloat(document.getElementById("tester-slippage").value) || 1.0,
    priority_fee: parseFloat(document.getElementById("tester-priority-fee").value) || 0.0001,
    bribe_fee: parseFloat(document.getElementById("tester-bribe-fee").value) || 0.000
  };
  const testerStr = `&buy_size=${testerConfig.buy_size_sol}&slippage_pct=${testerConfig.slippage_pct}&priority_fee=${testerConfig.priority_fee}&bribe_fee=${testerConfig.bribe_fee}`;

  engineParams = getEngineParams();
  const paramsStr = encodeURIComponent(JSON.stringify(engineParams));
  ws = new WebSocket(`${WS_BASE}/${mint}?timeframe=${timeframe}&params=${paramsStr}${testerStr}&engine_version=${engineVersion}`);
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
  engineParams = getEngineParams();

  // Update engine badge in settings modal
  const badge = document.getElementById("settings-engine-badge");
  if (badge) {
    badge.textContent = "V1";
    badge.className = "engine-badge";
  }

  // Hint text for specific params
  const paramHints = {
    stoploss_pct: "0 = off  |  negative = hard stop (-10 exits at -10% from entry)  |  positive = true trailing stop (10 exits if price falls 10% from its absolute peak since entry)",
    takeprofit_pct: "Take profit at this % gain (0 = disabled, exits position when price hits entry * (1 + pct/100))",
    takeprofit_pct_low: "TP% used when confidence ≤ confidence_low — tighter exit at low conviction (0 = disabled)",
    takeprofit_pct_high: "TP% used when confidence ≥ confidence_high — let winners run at high conviction (0 = disabled)",
    stoploss_pct_low: "SL magnitude (%) at low confidence — wider stop when conviction is low (0 = disabled)",
    stoploss_pct_high: "SL magnitude (%) at high confidence — tighter stop when conviction is high (0 = disabled)",
    confidence_high: "EXIT: exit regime filter upper threshold — below this confidence the regime is 'ambiguous' and no new signals fire (also used as upper bound for TP/SL lerp)",
    confidence_low: "EXIT: exit regime filter lower threshold — below this confidence the engine forces EXHAUSTION and exits (also used as lower bound for TP/SL lerp)",
    entry_confidence_high: "ENTRY: minimum confidence required to open a new position (independent of exit thresholds)",
    entry_confidence_low: "ENTRY: lower confidence floor for entry — entries are blocked below this level (currently a hard gate, not a lerp)",
    breakout_pct: "Buy when price > VWAP × (1 + breakout_pct/100)",
    vol_spike_mult: "Volume must exceed this × average volume to confirm entry",
    roc_min_pct: "Minimum Rate of Change % to trigger a buy signal",
    trailing_stop_pct: "Trail a stop this % below peak since entry (activates once in profit)",
    hard_stop_pct: "Fixed stop loss: exit if price drops this % from entry",
    max_hold_bars: "Maximum bars to hold a position (0 = disabled)",
    take_profit_pct: "Take profit at this % gain (0 = disabled, use trailing stop)",
    cooldown_bars: "After an exit, wait this many bars before re-entering",
    roc_exit_bars: "Exit if ROC stays negative for this many consecutive bars",
    rsi_overbought: "Block entries when RSI exceeds this threshold",
  };
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

    // Append hint if available, and span full width for readability
    if (paramHints[key]) {
      const hint = document.createElement("span");
      hint.className = "param-hint";
      hint.textContent = paramHints[key];
      group.append(hint);
      group.classList.add("full-width");
    }

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
  engineParams = getEngineParams();
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
  // Phantom wallet auto-refresh removed
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
  let stopBtn = "";
  if (rec.status === "recording") {
    stopBtn = `<button class="btn btn-danger btn-xs" style="margin-right:4px;" onclick="stopRecording(${rec.id}, event)">⏹ Stop</button>`;
  }
  const actions = opts.viewerMode
    ? `${stopBtn}<button class="btn btn-primary btn-sm" onclick="loadViewer(${rec.id})">📊 View Chart</button>
       <button class="btn btn-danger btn-xs" style="margin-left:4px;" onclick="deleteRecording(${rec.id}, event)">🗑</button>`
    : `${stopBtn}<button class="btn btn-danger btn-xs" onclick="deleteRecording(${rec.id}, event)">🗑</button>`;
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

async function stopRecording(id, e) {
  if (e) e.stopPropagation();
  await apiFetch("/api/recorder/stop", { method: "POST", body: JSON.stringify({ recording_id: id }) });
  checkRecorderStatus();
  loadRecordingsList("recordings-list");
  loadRecordingsList("viewer-recordings-list", true);
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
  if (data.active && data.recordings && data.recordings.length) {
    const rec = data.recordings[0];
    statusEl.classList.remove("hidden");
    startBtn.classList.remove("hidden");
    const nameStr = (rec.token_name || rec.mint?.slice(0, 8) || "—") + (rec.token_symbol ? ` $${rec.token_symbol}` : "");
    document.getElementById("rec-status-mint").textContent =
      data.count > 1 ? `${nameStr} +${data.count - 1} more` : nameStr;
    document.getElementById("rec-status-tf").textContent = rec.timeframe;
    document.getElementById("rec-status-candles").textContent = `${rec.candle_count} candles`;
    if (!recPollTimer) recPollTimer = setInterval(checkRecorderStatus, 3000);
  } else {
    statusEl.classList.add("hidden");
    startBtn.classList.remove("hidden");
    if (recPollTimer) { clearInterval(recPollTimer); recPollTimer = null; }
  }
}

document.getElementById("rec-start-btn").addEventListener("click", async () => {
  const mint = document.getElementById("rec-mint-input").value.trim();
  if (!mint) return alert("Enter a token address");
  const tf = document.getElementById("rec-tf-select").value;
  const data = await apiFetch("/api/recorder/start", { method: "POST", body: JSON.stringify({ mint, timeframe: tf }) });
  if (data.error) return alert(data.error);
  document.getElementById("rec-mint-input").value = "";
  checkRecorderStatus();
  loadRecordingsList("recordings-list");
});

/* ── Offline Chart Formatter ─────────── */

async function formatOfflineCandles(mint, rawCandles, timeframeStr) {
  if (!rawCandles || !rawCandles.length) return { candles: [], currency: "SOL" };

  // Find first non-zero open price across all candles to use as base
  let basePrice = 0;
  for (const c of rawCandles) {
    if (c.open > 0) { basePrice = c.open; break; }
    if (c.close > 0) { basePrice = c.close; break; }
  }
  if (!basePrice || basePrice <= 0) basePrice = 1; // last-resort fallback

  let baseMcap = 0;
  let ccy = "SOL";

  try {
    const tInfo = await apiFetch(`/api/token/${mint}`);
    if (tInfo && !tInfo.error) {
      // Determine SOL/USD price
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

  const toMcap = (p, fallback) => {
    if (!p || p <= 0 || isNaN(p)) return fallback !== undefined ? fallback : 0;
    const v = baseMcap * (p / basePrice);
    if (!isFinite(v) || isNaN(v) || v <= 0) return fallback !== undefined ? fallback : 0;
    return v;
  };

  const tfSec = timeframeToSeconds(timeframeStr);
  const formatted = [];
  let lastTime = null;
  let lastClose = null;
  const seenTimes = new Set(); // deduplicate by time

  for (const c of rawCandles) {
    // Gap fill
    if (lastTime !== null && lastClose !== null && c.time > lastTime + tfSec) {
      const gap = Math.floor((c.time - lastTime) / tfSec) - 1;
      if (gap <= 15) {
        for (let t = lastTime + tfSec; t < c.time; t += tfSec) {
          if (!seenTimes.has(t)) {
            seenTimes.add(t);
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
    }

    if (seenTimes.has(c.time)) continue; // skip duplicate timestamps
    seenTimes.add(c.time);

    const closeVal = toMcap(c.close, lastClose || toMcap(c.open, null));
    if (closeVal === null || closeVal <= 0) continue; // skip unrenderable candle

    let open = lastClose !== null ? lastClose : toMcap(c.open, closeVal);
    let high = toMcap(c.high, closeVal);
    let low = toMcap(c.low, closeVal);
    const close = closeVal;

    // Ensure OHLC is consistent
    high = Math.max(open, high, close);
    low = Math.min(open, low, close);

    let color = CANDLE_FLAT;
    if (close > open) color = CANDLE_UP;
    else if (close < open) color = CANDLE_DOWN;
    else if (lastClose !== null) {
      if (close > lastClose) color = CANDLE_UP;
      else if (close < lastClose) color = CANDLE_DOWN;
    }

    // Preserve backtest-specific fields (trade_action, trade_label, regime…) but only pass OHLCV + color to chart
    formatted.push({
      ...c,         // keep trade_action/trade_label for marker logic
      time: c.time,
      open, high, low, close,
      volume: c.volume || 0,
      color, borderColor: color, wickColor: color,
    });
    lastTime = c.time;
    lastClose = close;
  }

  return { candles: formatted, currency: ccy, baseMcap, basePrice, lastClose, lastTime };
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

  const singleTests = [];
  const batches = {};

  for (const bt of list) {
    if (bt.batch_id) {
      if (!batches[bt.batch_id]) batches[bt.batch_id] = [];
      batches[bt.batch_id].push(bt);
    } else {
      singleTests.push(bt);
    }
  }

  let html = "";

  const batchGroups = Object.entries(batches).sort((a, b) => b[1][0].created_at - a[1][0].created_at);
  for (const [bId, bItems] of batchGroups) {
    let totalTrades = 0;
    let winningTrades = 0;
    let totalPnl = 0;

    for (const bt of bItems) {
      const trades = bt.total_trades || 0;
      totalTrades += trades;
      winningTrades += Math.round(trades * (bt.win_rate || 0) / 100);
      totalPnl += (bt.total_pnl || 0);
    }

    const overallWinRate = totalTrades > 0 ? (winningTrades / totalTrades * 100) : 0;
    const pnlClass = totalPnl >= 0 ? "pos" : "neg";
    const pnlSign = totalPnl >= 0 ? "+" : "";

    html += `
    <div class="backtest-card batch-folder" onclick="toggleBatchFolder('${bId}')" style="border-left: 4px solid #5865f2; cursor: pointer;">
      <div class="bt-card-header">
        <div><span class="bt-card-name">📁 Batch Run</span> <span class="rec-card-symbol">${bItems.length} coins</span></div>
        <div class="rec-card-badges"><span class="rec-card-badge">Batch</span></div>
      </div>
      <div class="bt-card-stats">
        <div class="bt-stat"><span class="bt-stat-label">Total Trades</span><span class="bt-stat-value">${totalTrades}</span></div>
        <div class="bt-stat"><span class="bt-stat-label">Win Rate</span><span class="bt-stat-value">${overallWinRate.toFixed(1)}%</span></div>
        <div class="bt-stat"><span class="bt-stat-label">Total PnL</span><span class="bt-stat-value ${pnlClass}">${pnlSign}${totalPnl.toFixed(4)} SOL</span></div>
      </div>
      <div class="rec-card-details" style="margin-top:8px"><span>🕐 ${fmtTs(bItems[0].created_at)}</span></div>
      <div class="bt-card-actions"><button class="btn btn-danger btn-xs" onclick="deleteBatch('${bId}', event)">🗑 Delete Batch</button></div>
    </div>
    <div id="batch-items-${bId}" class="batch-items-container hidden" style="margin-left:20px; border-left: 2px dashed #30363d; padding-left:10px; margin-bottom: 10px; display: none;">
      ${bItems.map(bt => renderSingleBacktestCard(bt)).join("")}
    </div>
    `;
  }

  html += singleTests.map(bt => renderSingleBacktestCard(bt)).join("");
  el.innerHTML = html;
}

function renderSingleBacktestCard(bt) {
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
        <div class="bt-stat"><span class="bt-stat-label">Win Rate</span><span class="bt-stat-value">${(bt.win_rate || 0).toFixed(1)}%</span></div>
        <div class="bt-stat"><span class="bt-stat-label">PnL</span><span class="bt-stat-value ${pnlClass}">${pnlSign}${(bt.total_pnl || 0).toFixed(4)}</span></div>
      </div>
      <div class="rec-card-details" style="margin-top:8px"><span>🕐 ${fmtTs(bt.created_at)}</span></div>
      <div class="bt-card-actions"><button class="btn btn-danger btn-xs" onclick="deleteBacktest(${bt.id}, event)">🗑</button></div>
    </div>`;
}

window.toggleBatchFolder = function (bId) {
  const el = document.getElementById(`batch-items-${bId}`);
  if (el) {
    if (el.style.display === "none" || el.classList.contains("hidden")) {
      el.style.display = "flex";
      el.style.flexDirection = "column";
      el.style.gap = "12px";
      el.classList.remove("hidden");
    } else {
      el.style.display = "none";
      el.classList.add("hidden");
    }
  }
};

async function deleteBatch(bId, e) {
  if (e) e.stopPropagation();
  if (!confirm("Delete this entire batch?")) return;
  await apiFetch(`/api/backtests/batch/${bId}`, { method: "DELETE" });
  loadBacktestsList();
}

async function deleteBacktest(id, e) {
  if (e) e.stopPropagation();
  await apiFetch(`/api/backtests/${id}`, { method: "DELETE" });
  loadBacktestsList();
}

async function deleteAllBacktests() {
  if (!confirm("Are you sure you want to delete ALL backtests?")) return;
  await apiFetch(`/api/backtests`, { method: "DELETE" });
  loadBacktestsList();
}

document.getElementById("bt-run-btn").addEventListener("click", async () => {
  const recId = document.getElementById("bt-recording-select").value;
  if (!recId) return alert("Select a recording first");
  const prog = document.getElementById("bt-progress");
  prog.classList.remove("hidden");
  document.getElementById("bt-run-btn").disabled = true;

  const testerConfig = {
    buy_size_sol: parseFloat(document.getElementById("tester-buy-size").value) || 0.1,
    slippage_pct: parseFloat(document.getElementById("tester-slippage").value) || 1.0,
    priority_fee: parseFloat(document.getElementById("tester-priority-fee").value) || 0.0001,
    bribe_fee: parseFloat(document.getElementById("tester-bribe-fee").value) || 0.00001
  };

  try {
    const result = await apiFetch("/api/backtest", {
      method: "POST",
      body: JSON.stringify({
        recording_id: parseInt(recId),
        engine_params: getEngineParams(),
        engine_version: engineVersion,
        ...testerConfig
      })
    });
    if (result.error) { alert(result.error); return; }
    loadBacktestsList();
    loadBacktestResult(result.backtest_id);
  } finally {
    prog.classList.add("hidden");
    document.getElementById("bt-run-btn").disabled = false;
  }
});

document.getElementById("bt-run-all-btn").addEventListener("click", async () => {
  const recordings = await apiFetch("/api/recordings");
  const completed = recordings.filter(r => r.status === "completed");
  if (!completed.length) return alert("No completed recordings to backtest.");

  const prog = document.getElementById("bt-progress");
  const progLabel = document.getElementById("bt-progress-label");
  const runAllBtn = document.getElementById("bt-run-all-btn");
  const runBtn = document.getElementById("bt-run-btn");
  prog.classList.remove("hidden");
  runAllBtn.disabled = true;
  runBtn.disabled = true;
  progLabel.textContent = `Running all ${completed.length} recordings in parallel…`;

  try {
    const testerConfig = {
      buy_size_sol: parseFloat(document.getElementById("tester-buy-size").value) || 0.1,
      slippage_pct: parseFloat(document.getElementById("tester-slippage").value) || 1.0,
      priority_fee: parseFloat(document.getElementById("tester-priority-fee").value) || 0.0001,
      bribe_fee: parseFloat(document.getElementById("tester-bribe-fee").value) || 0.00001
    };

    const result = await apiFetch("/api/backtest/batch", {
      method: "POST",
      body: JSON.stringify({
        engine_params: getEngineParams(),
        engine_version: engineVersion,
        ...testerConfig
      }),
    });
    const msg = `Done: ${result.succeeded}/${result.total} backtests succeeded.`;
    if (result.failed > 0) alert(msg);
    loadBacktestsList();
  } catch (e) {
    alert(`Batch backtest failed: ${e.message || e}`);
  } finally {
    prog.classList.add("hidden");
    runAllBtn.disabled = false;
    runBtn.disabled = false;
    progLabel.textContent = "Running…";
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
window.deleteAllBacktests = deleteAllBacktests;
window.deleteBatch = deleteBatch;

/* ════════════════════════════════════════════════════════════════════════
   LIVE TRADING — Real on-chain execution via Phantom + Jupiter
   ════════════════════════════════════════════════════════════════════════ */

const JUPITER_QUOTE = "https://lite-api.jup.ag/swap/v1/quote";
const JUPITER_SWAP = "https://lite-api.jup.ag/swap/v1/swap";
const WSOL = "So11111111111111111111111111111111111111112";
const SOL_DECIMALS = 9;
const LT_WS_BASE = `ws://${location.host}/ws/live`;

/* ── State ────────────────────────────────────────────────────────────── */

let ltWalletPubkey = null;
let ltWalletConnected = false;
const ltActiveTraders = {};  // mint -> { ws, info, events[] }
let ltTradeCounter = 0;

/* ── DOM refs ─────────────────────────────────────────────────────────── */

const ltConnectBtn = $("lt-connect-btn");
const ltWalletDot = $("lt-wallet-dot");
const ltWalletLabel = $("lt-wallet-label");
const ltWalletAddr = $("lt-wallet-addr");
const ltWalletBal = $("lt-wallet-bal");
const ltAddBtn = $("lt-add-btn");
const ltStopAllBtn = $("lt-stop-all-btn");
const ltTokenInput = $("lt-token-input");
const ltTradersGrid = $("lt-traders-grid");
const ltTradesTbody = $("lt-trades-tbody");

/* ── Wallet Setup (Private Key) ────────────────────────────────────────── */

let _privateKey = "";

function connectWallet() {
  const pkInput = $("lt-private-key").value.trim();
  if (!pkInput) return alert("Please enter your base58 private key.");
  if (pkInput.length < 32) return alert("Private key seems too short. Expected a base58 string.");

  _privateKey = pkInput;
  ltWalletPubkey = "connected";
  ltWalletConnected = true;

  ltWalletDot.className = "dot connected";
  ltWalletLabel.textContent = "Key Set";
  ltWalletAddr.textContent = "(Server-side signing)";
  ltWalletBal.textContent = "";
  ltConnectBtn.textContent = "✅ Key Saved";
  ltAddBtn.disabled = false;

  $("lt-private-key").value = "";
  $("lt-private-key").placeholder = "Key securely set in memory.";
}

ltConnectBtn.addEventListener("click", connectWallet);

/* ── Get config values ───────────────────────────────────────────────── */

function getLtConfig() {
  return {
    buySize: parseFloat($("lt-buy-size").value) || 0.1,
    slippagePct: parseFloat($("lt-slippage").value) || 10,
    slippageBps: Math.round((parseFloat($("lt-slippage").value) || 10) * 100),
    priorityFeeSol: parseFloat($("lt-priority-fee").value) || 0.0001,
    priorityFeeLamports: Math.round((parseFloat($("lt-priority-fee").value) || 0.0001) * 1e9),
    timeframe: $("lt-timeframe").value,
  };
}

/* ── Legacy swap handler removed (moves to backend) ───────────────── */

/* ── Trader event log ────────────────────────────────────────────────── */

function addTraderEvent(ctx, type, msg) {
  const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  ctx.events.unshift({ type, msg, ts });
  if (ctx.events.length > 50) ctx.events.pop();
  updateTraderCard(ctx.mint);
}

/* ── Trade history table ─────────────────────────────────────────────── */

function addLtTradeRow(ctx, action, price, pnlSol, pnlPct, txHash, status) {
  ltTradeCounter++;
  const tr = document.createElement("tr");
  const pnlClass = pnlSol >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
  const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const solscanUrl = `https://solscan.io/tx/${txHash}`;
  tr.innerHTML = `
    <td>${ltTradeCounter}</td>
    <td>${ctx.info?.token_symbol ? "$" + ctx.info.token_symbol : ctx.mint.slice(0, 6) + "…"}</td>
    <td style="color:${action === "BUY" ? "var(--green)" : "var(--red)"}; font-weight:700">${action}</td>
    <td>${ts}</td>
    <td>${price ? price.toExponential(4) : "—"}</td>
    <td class="${pnlClass}">${pnlSol ? (pnlSol >= 0 ? "+" : "") + pnlSol.toFixed(6) : "—"}</td>
    <td class="${pnlClass}">${pnlPct ? (pnlPct >= 0 ? "+" : "") + pnlPct.toFixed(2) + "%" : "—"}</td>
    <td><a href="${solscanUrl}" target="_blank" style="color:var(--accent);text-decoration:none">${txHash.slice(0, 8)}…</a></td>
    <td>${status}</td>
  `;
  ltTradesTbody.prepend(tr);
}

/* ── Trader card rendering ───────────────────────────────────────────── */

function updateTraderCard(mint) {
  const ctx = ltActiveTraders[mint];
  if (!ctx) return;

  let card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
  let isNew = false;
  if (!card) {
    card = document.createElement("div");
    card.className = "lt-trader-card";
    card.dataset.mint = mint;

    // Remove empty state if present
    const empty = ltTradersGrid.querySelector(".empty-state");
    if (empty) empty.remove();

    card.innerHTML = `
      <div class="lt-card-header">
        <div><span class="lt-card-name" id="lth-name-${mint}"></span><span class="lt-card-symbol" id="lth-sym-${mint}"></span></div>
        <div style="display:flex;gap:6px;align-items:center">
          <div id="lth-trend-${mint}" class="direction-badge" style="font-size:10px; padding:2px 6px; display:none"></div>
          <div id="lth-regime-${mint}" class="regime-badge" style="font-size:10px; padding:2px 6px; display:none"></div>
          <div id="lth-status-${mint}"></div>
        </div>
      </div>
      <div class="lt-card-stats" id="lt-stats-${mint}"></div>
      <div id="lt-upnl-${mint}"></div>
      <div class="lt-card-chart-container" id="lt-chart-${mint}" style="height:250px; margin:10px 0; border:1px solid var(--border); border-radius:6px; background:#0d1117"></div>
      <div class="lt-event-log" id="lt-events-${mint}"></div>
      <div class="lt-card-actions" style="display:flex; gap:8px;">
        <button class="btn btn-primary btn-xs" style="background:#26a69a; border-color:#26a69a" onclick="manualTrade('${mint}', 'buy')">Buy</button>
        <button class="btn btn-primary btn-xs" style="background:#ef5350; border-color:#ef5350" onclick="manualTrade('${mint}', 'sell')">Sell</button>
        <button class="btn btn-danger btn-xs" onclick="stopLiveTrader('${mint}')" style="margin-left:auto;">⏹ Stop</button>
      </div>
    `;
    ltTradersGrid.appendChild(card);
    isNew = true;

    // Init lightweight charts
    const chart = LightweightCharts.createChart(document.getElementById(`lt-chart-${mint}`), {
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8b949e', fontSize: 11 },
      grid: { vertLines: { color: '#30363d33' }, horzLines: { color: '#30363d33' } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: true },
      crosshair: { mode: 0 }
    });
    const cSeries = chart.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      priceFormat: { type: 'custom', minMove: 1, formatter: p => formatMcap(p) }
    });
    const vSeries = chart.addHistogramSeries({
      color: '#5865f222', priceFormat: { type: 'volume' }, priceScaleId: 'vol'
    });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    ctx.chart = chart;
    ctx.candleSeries = cSeries;
    ctx.volSeries = vSeries;

    new ResizeObserver(() => {
      const el = document.getElementById(`lt-chart-${mint}`);
      if (el) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    }).observe(document.getElementById(`lt-chart-${mint}`));
  }

  const st = ctx.stats || {};
  const ct = ctx.currentTrade;
  const hasPos = !!ct;
  card.className = `lt-trader-card${hasPos ? " has-position" : ""}`;

  const pnlClass = (st.total_pnl_sol || 0) >= 0 ? "pos" : "neg";
  const upnlClass = (ctx.unrealizedPnl || 0) >= 0 ? "pos" : "neg";

  const name = ctx.info?.token_name || mint.slice(0, 8);
  const symbol = ctx.info?.token_symbol ? "$" + ctx.info.token_symbol : "";
  document.getElementById(`lth-name-${mint}`).textContent = name;
  document.getElementById(`lth-sym-${mint}`).textContent = symbol;
  document.getElementById(`lth-status-${mint}`).innerHTML = hasPos ? '<span class="lt-card-status position-open">IN POSITION</span>' : '<span class="lt-card-status running"><span class="lt-live-dot"></span>MONITORING</span>';

  // Badges
  const tb = document.getElementById(`lth-trend-${mint}`);
  const rb = document.getElementById(`lth-regime-${mint}`);
  if (ctx.direction && ctx.direction !== "none") {
    tb.style.display = "block";
    const arrow = ctx.direction === "up" ? "▲" : "▼";
    const tColor = ctx.direction === "up" ? CANDLE_UP : CANDLE_DOWN;
    tb.style.color = tColor;
    tb.textContent = `${arrow} ${ctx.direction.toUpperCase()} S:${(ctx.sVal || 0).toFixed(2)}`;
  } else {
    tb.style.display = "none";
  }

  if (ctx.regime && ctx.regime !== "idle") {
    rb.style.display = "block";
    rb.textContent = ctx.regime.toUpperCase();
    rb.style.background = REGIME_COLORS[ctx.regime] || "#5a6071";
  } else {
    rb.style.display = "none";
  }

  document.getElementById(`lt-stats-${mint}`).innerHTML = `
    <div class="bt-stat"><span class="bt-stat-label">Trades</span><span class="bt-stat-value">${st.total_trades || 0}</span></div>
    <div class="bt-stat"><span class="bt-stat-label">Win Rate</span><span class="bt-stat-value">${(st.win_rate || 0).toFixed(1)}%</span></div>
    <div class="bt-stat"><span class="bt-stat-label">PnL</span><span class="bt-stat-value ${pnlClass}">${(st.total_pnl_sol || 0) >= 0 ? "+" : ""}${(st.total_pnl_sol || 0).toFixed(4)}</span></div>
  `;

  document.getElementById(`lt-upnl-${mint}`).innerHTML = hasPos ? `
    <div class="lt-card-unrealized">
      <span>Unrealized PnL</span>
      <span class="${upnlClass}" style="font-weight:700">${(ctx.unrealizedPnl || 0) >= 0 ? "+" : ""}${(ctx.unrealizedPnl || 0).toFixed(4)} SOL (${(ctx.unrealizedPnlPct || 0) >= 0 ? "+" : ""}${(ctx.unrealizedPnlPct || 0).toFixed(2)}%)</span>
    </div>
  ` : "";

  document.getElementById(`lt-events-${mint}`).innerHTML = ctx.events.slice(0, 5).map(e =>
    `<div class="${e.type}">${e.ts} — ${e.msg}</div>`
  ).join("");
}

function manualTrade(mint, action) {
  const ctx = ltActiveTraders[mint];
  if (!ctx || ctx.ws.readyState !== WebSocket.OPEN) return;
  ctx.ws.send(JSON.stringify({ type: "manual_trade", action: action }));
  addTraderEvent(ctx, "info", `Manual ${action.toUpperCase()} requested…`);
}
window.manualTrade = manualTrade;

/* ── Start live trading on a token ───────────────────────────────────── */

// Stagger consecutive startLiveTrader calls so N parallel tokens don't all
// open their WebSockets (and trigger resolve_input) at the exact same moment.
let _ltConnectCount = 0;
let _ltConnectResetTimer = null;

function startLiveTrader(mint, _delayOverride = null) {
  if (!ltWalletPubkey) return alert("Connect wallet first");
  if (ltActiveTraders[mint]) return alert("Already trading this token");
  if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint)) return alert("Invalid Solana address");

  // Stagger: each successive call within 2s adds 400ms extra delay
  const delayMs = _delayOverride !== null ? _delayOverride : _ltConnectCount * 400;
  _ltConnectCount++;
  clearTimeout(_ltConnectResetTimer);
  _ltConnectResetTimer = setTimeout(() => { _ltConnectCount = 0; }, 2000);

  const config = getLtConfig();
  const paramsStr = encodeURIComponent(JSON.stringify(getEngineParams()));
  const wsUrl = `${LT_WS_BASE}/${mint}?timeframe=${config.timeframe}&private_key=${encodeURIComponent(_privateKey)}&buy_size=${config.buySize}&slippage_bps=${config.slippageBps}&priority_fee=${config.priorityFeeLamports}&params=${paramsStr}&engine_version=${engineVersion}`;

  // Register the card immediately so the UI shows "Connecting…" right away
  const ctx = {
    mint,
    ws: null,  // filled in after delay
    info: null,
    stats: {},
    currentTrade: null,
    unrealizedPnl: 0,
    unrealizedPnlPct: 0,
    events: [],
    regime: "idle",
    direction: "none",
    sVal: 0,
  };
  ltActiveTraders[mint] = ctx;
  ltStopAllBtn.style.display = "inline-flex";
  addTraderEvent(ctx, "info", delayMs > 0 ? `Connecting… (staggered ${delayMs}ms)` : "Connecting…");
  updateTraderCard(mint);

  setTimeout(() => {
    if (!ltActiveTraders[mint]) return;  // was stopped before delay elapsed
    const ws = new WebSocket(wsUrl);
    ctx.ws = ws;

    ws.onopen = () => { addTraderEvent(ctx, "info", "Connected — warming up indicators…"); };

    ws.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }

      if (msg.type === "token_info") {
        ctx.info = msg.data;
        addTraderEvent(ctx, "info", `Token: ${msg.data.name || mint.slice(0, 8)}`);
        updateTraderCard(mint);
      }

      if (msg.type === "historical" && msg.strategy) {
        addTraderEvent(ctx, "info", `Loaded ${msg.candles?.length || 0} historical candles`);
        if (ctx.candleSeries && msg.candles) {
          const res = await formatOfflineCandles(mint, msg.candles, config.timeframe);
          ctx.baseMcap = res.baseMcap;
          ctx.basePrice = res.basePrice;
          ctx.lastClose = res.lastClose;
          ctx.lastTime = res.lastTime;

          ctx.candleSeries.setData(res.candles);
          ctx.volSeries.setData(res.candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.color })));
          if (ctx.chart) ctx.chart.timeScale().fitContent();
        }
        if (Array.isArray(msg.strategy) && msg.strategy.length > 0) {
          const lastS = msg.strategy[msg.strategy.length - 1];
          ctx.regime = lastS.regime || "idle";
          ctx.direction = lastS.direction || "none";
          ctx.sVal = lastS.s || 0;
          updateTraderCard(mint);
        }
      }

      if (msg.type === "candle" && msg.strategy) {
        const s = msg.strategy;
        if (ctx.candleSeries && msg.candle) {
          if (!ctx.baseMcap) {
            ctx.baseMcap = msg.market_cap_usd || (msg.candle.close * 1e9);
            ctx.basePrice = msg.candle.close;
            ctx.lastClose = null;
            ctx.lastTime = null;
          }

          let rawOpen = ctx.baseMcap * (msg.candle.open / ctx.basePrice);
          let high = ctx.baseMcap * (msg.candle.high / ctx.basePrice);
          let low = ctx.baseMcap * (msg.candle.low / ctx.basePrice);
          let close = ctx.baseMcap * (msg.candle.close / ctx.basePrice);

          // Gap filling and new candle bridging
          if (msg.is_new) {
            const tfSec = timeframeToSeconds(config.timeframe);
            if (ctx.lastTime && msg.candle.time > ctx.lastTime + tfSec) {
              const gap = Math.floor((msg.candle.time - ctx.lastTime) / tfSec) - 1;
              if (gap <= 15) {
                for (let t = ctx.lastTime + tfSec; t < msg.candle.time; t += tfSec) {
                  ctx.candleSeries.update({ time: t, open: ctx.lastClose, high: ctx.lastClose, low: ctx.lastClose, close: ctx.lastClose, color: CANDLE_FLAT, borderColor: CANDLE_FLAT, wickColor: CANDLE_FLAT });
                  ctx.volSeries.update({ time: t, value: 0, color: "#5865f222" });
                }
              }
            }
            ctx.currentOpen = ctx.lastClose !== null && ctx.lastClose !== undefined ? ctx.lastClose : rawOpen;
          }

          let open = ctx.currentOpen !== undefined ? ctx.currentOpen : rawOpen;
          low = Math.min(open, low, close);

          let color = CANDLE_FLAT;
          if (close > open) color = CANDLE_UP;
          else if (close < open) color = CANDLE_DOWN;
          else if (ctx.lastClose !== null && ctx.lastClose !== undefined) {
            if (close > ctx.lastClose) color = CANDLE_UP;
            else if (close < ctx.lastClose) color = CANDLE_DOWN;
          }

          ctx.candleSeries.update({ time: msg.candle.time, open, high, low, close, color, borderColor: color, wickColor: color });
          ctx.volSeries.update({ time: msg.candle.time, value: msg.candle.volume || 0, color: color });

          ctx.lastClose = close;
          ctx.lastTime = msg.candle.time;
        }

        // Update live_trade data
        const lt = s.live_trade || s.forward_test;
        if (lt) {
          ctx.stats = lt.stats || ctx.stats;
          ctx.currentTrade = lt.current_trade;
          ctx.unrealizedPnl = lt.unrealized_pnl || 0;
          ctx.unrealizedPnlPct = lt.unrealized_pnl_pct || 0;
        }
        ctx.regime = s.regime || "idle";
        ctx.direction = s.direction || "none";
        ctx.sVal = s.s || 0;
        updateTraderCard(mint);
      }

      if (msg.type === "trade_update") {
        ctx.stats = msg.stats || ctx.stats;
        ctx.currentTrade = msg.current_trade;
        if (msg.event === "buy_confirmed" || msg.event === "sell_confirmed") {
          const sig = msg.detail || "";
          const shortSig = sig.length > 10 ? sig.slice(0, 10) + "…" : sig;
          const action = msg.event.includes("buy") ? "buy" : "sell";
          addTraderEvent(ctx, action, `${msg.event.replace("_", " ").toUpperCase()} ✓ ${shortSig}`);
          if (msg.event === "buy_confirmed") {
            addLtTradeRow(ctx, "BUY", msg.current_trade?.entry_price || 0, 0, 0, sig, "confirmed");
          }
          if (msg.event === "sell_confirmed") {
            const ct = msg.closed_trade || msg.current_trade;
            if (ct) {
              addLtTradeRow(ctx, "SELL", ct.exit_price || 0, ct.pnl_sol || 0, ct.pnl_pct || 0, sig, "confirmed");
            }
            if (msg.sol_received) {
              addTraderEvent(ctx, "sell", `Received ${msg.sol_received.toFixed(6)} SOL`);
            }
          }
        } else if (msg.event === "buy_failed" || msg.event === "sell_failed") {
          addTraderEvent(ctx, "error", `❌ ${msg.event.replace("_", " ").toUpperCase()}: ${msg.detail}`);
        } else if (msg.event === "tx_simulation_failed") {
          addTraderEvent(ctx, "error", `⚠️ SIMULATION FAILED: ${msg.detail}`);
        } else if (msg.event === "mcap_stop") {
          addTraderEvent(ctx, "error", `🛑 MCAP FLOOR: ${msg.detail}`);
          const card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
          if (card) { card.style.borderColor = "var(--red)"; card.style.opacity = "0.7"; }
          setTimeout(() => stopLiveTrader(mint), 8000);
        } else if (msg.event === "no_motion_stop") {
          addTraderEvent(ctx, "error", `🕐 NO MOTION: ${msg.detail}`);
          const card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
          if (card) { card.style.borderColor = "var(--red)"; card.style.opacity = "0.7"; }
          setTimeout(() => stopLiveTrader(mint), 8000);
        }
        updateTraderCard(mint);
      }

      if (msg.type === "ping") {
        ws.send(JSON.stringify({ type: "pong" }));
      }
    };

    ws.onerror = () => { addTraderEvent(ctx, "error", "WebSocket error"); };
    ws.onclose = () => {
      addTraderEvent(ctx, "info", "Disconnected");
      updateTraderCard(mint);
    };
  }, delayMs);
}

/* ── Stop trader ─────────────────────────────────────────────────────── */

function stopLiveTrader(mint) {
  const ctx = ltActiveTraders[mint];
  if (!ctx) return;
  if (ctx.ws) ctx.ws.close();
  apiFetch("/api/live/stop", { method: "POST", body: JSON.stringify({ mint }) }).catch(() => { });
  delete ltActiveTraders[mint];
  const card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
  if (card) card.remove();
  if (Object.keys(ltActiveTraders).length === 0) {
    ltTradersGrid.innerHTML = '<div class="empty-state">No active traders. Connect wallet and add tokens above.</div>';
    ltStopAllBtn.style.display = "none";
  }
}

function stopAllTraders() {
  for (const mint of Object.keys(ltActiveTraders)) {
    stopLiveTrader(mint);
  }
}

/* ── Event listeners ─────────────────────────────────────────────────── */


ltAddBtn.addEventListener("click", () => {
  const mint = ltTokenInput.value.trim();
  if (!mint) return;
  startLiveTrader(mint);
  ltTokenInput.value = "";
});

ltTokenInput.addEventListener("keydown", e => { if (e.key === "Enter") ltAddBtn.click(); });
ltStopAllBtn.addEventListener("click", stopAllTraders);

/* Config change listeners — hot-update all active traders */
["lt-buy-size", "lt-slippage", "lt-priority-fee"].forEach(id => {
  $(id).addEventListener("change", () => {
    const config = getLtConfig();
    for (const ctx of Object.values(ltActiveTraders)) {
      if (ctx.ws && ctx.ws.readyState === WebSocket.OPEN) {
        ctx.ws.send(JSON.stringify({
          type: "update_config",
          buy_size: config.buySize,
          slippage_bps: config.slippageBps,
          priority_fee: config.priorityFeeLamports,
        }));
      }
    }
  });
});

// Page switch handler for live trading
const origSwitchPage = switchPage;
switchPage = function (pageId) {
  origSwitchPage(pageId);
  if (pageId === "live-trading" && ltWalletConnected) {
    refreshWalletBalance();
  }
};
// Re-bind nav tabs with new switchPage
navTabs.forEach(tab => {
  tab.removeEventListener("click", () => { });
  tab.addEventListener("click", () => switchPage(tab.dataset.page));
});


window.stopLiveTrader = stopLiveTrader;
window.stopAllTraders = stopAllTraders;


/* ══════════════════════════════════════════════════════════════════════════
   AUTOFEED — Auto-feed clean/organic pump.fun-migrated tokens from gmgn.ai
   ─────────────────────────────────────────────────────────────────────────
   Autofeed is discovery-only.  The backend polls gmgn-cli `market trending`
   with strict organic / non-bundled / mcap ≥ 15k gates.  Each accepted
   candidate is pushed over /ws/autofeed.  On each candidate we call
   startLiveTrader(mint) — exactly the same path the manual "Start Trading"
   button uses to open /ws/live/{mint}.

   Switch governance: the autofeed toggle cannot be turned on until the
   wallet key is set (`ltWalletConnected`).  We also force the backend to
   refuse if no private key is set, by sending `connected: true` on enable.
   ══════════════════════════════════════════════════════════════════════════ */

const AF_WS_BASE = `ws://${location.host}/ws/autofeed`;

/* ── DOM refs ────────────────────────────────────────────────────────── */
const afToggle = $("af-toggle");
const afSettingsWrap = $("af-settings");
const afStatusDot = $("af-status-dot");
const afStatusText = $("af-status-text");
const afCliStatus = $("af-cli-status");
const afCandidates = $("af-candidates");
const afStatSeen = $("af-stat-seen");
const afStatFed = $("af-stat-fed");
const afStatTracked = $("af-stat-tracked");
const afStatLastPoll = $("af-stat-lastpoll");
const afSaveConfigBtn = $("af-save-config");
const afTestPollBtn = $("af-test-poll");
const afPreview = $("af-preview");

let afWS = null;
let afConfigCache = null;

/* ── Toggle governance ──────────────────────────────────────────────── */
function afUpdateToggleGate() {
  // Cannot be enabled until wallet (private key) is set
  if (!ltWalletConnected) {
    if (afToggle.checked) afToggle.checked = false;
    afToggle.disabled = true;
    afToggle.parentElement.title = "Connect wallet (set private key) first";
    if (!afToggle.checked) afSetStatus("off", "Wallet key required");
  } else {
    afToggle.disabled = false;
    afToggle.parentElement.title = "Turn on autofeed";
  }
}

/* ── WS lifecycle ───────────────────────────────────────────────────── */
function afConnectWS() {
  if (afWS && afWS.readyState === WebSocket.OPEN) return;
  try {
    afWS = new WebSocket(AF_WS_BASE);
  } catch (e) { console.warn("[AutoFeed WS] connect failed", e); return; }

  afWS.onopen = () => afSetStatus("connected", "Connected");
  afWS.onclose = () => {
    afSetStatus("off", "Disconnected");
    afWS = null;
    // Reconnect in 5s only if autofeed is supposed to be on
    if (afToggle.checked && ltWalletConnected) {
      setTimeout(afConnectWS, 5000);
    }
  };
  afWS.onerror = () => afSetStatus("off", "WS error");

  afWS.onmessage = async (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "autofeed_status") {
      afHandleStatus(msg.data || {});
    } else if (msg.type === "autofeed_candidate") {
      afHandleCandidate(msg.candidate || {});
    } else if (msg.type === "ping") {
      try { afWS.send(JSON.stringify({ type: "pong" })); } catch { /* ignore */ }
    }
  };
}

function afDisconnectWS() {
  if (afWS) { try { afWS.close(); } catch { /* ignore */ } afWS = null; }
}

function afSetStatus(state, text) {
  afStatusDot.className = `dot ${state === "connected" ? "connected" : state === "running" ? "connected" : "error"}`;
  if (state === "running") afStatusDot.className = "dot connected";
  if (state === "off") afStatusDot.className = "dot error";
  if (text) afStatusText.textContent = text;
}

/* ── Incoming from backend ─────────────────────────────────────────── */
function afHandleStatus(snap) {
  afConfigCache = snap;
  // Ppopulate form fields from backend (only when local fields untouched -> just always)
  afPopulateForm(snap);

  // CLI configuration status
  if (snap.cli_configured) {
    afCliStatus.textContent = "gmgn-cli: ✅ configured";
    afCliStatus.style.color = "var(--green, #26a69a)";
  } else {
    afCliStatus.textContent = "gmgn-cli: ⚠ no API key";
    afCliStatus.style.color = "var(--red, #ef5350)";
  }

  // Stats
  afStatSeen.textContent = snap.total_seen || 0;
  afStatFed.textContent = snap.total_fed || 0;
  afStatTracked.textContent = snap.active_tracked || 0;
  if (snap.last_poll_at && snap.last_poll_at > 0) {
    afStatLastPoll.textContent = new Date(snap.last_poll_at * 1000).toLocaleTimeString();
  } else {
    afStatLastPoll.textContent = "—";
  }

  // Recent candidates → render
  afRenderCandidates(snap.recent_candidates || []);

  // Running state
  if (snap.is_running) {
    afSetStatus("running", "Running");
  } else {
    afSetStatus(snap.cli_configured ? "off" : "off", snap.cli_configured ? "Idle" : "gmgn-cli unconfigured");
  }
}

function afHandleCandidate(cand) {
  afAddCandidateRow(cand);
  // Feed into live trader — exactly the manual-button path.
  afFeedToLiveTrader(cand);
}

function afFeedToLiveTrader(cand) {
  if (!cand || !cand.mint) return;
  if (!ltWalletConnected) {
    console.warn("[AutoFeed] Received candidate but wallet not connected — skipping feed:", cand.mint);
    return;
  }
  // Don't double-start: skip if this mint is already an active trader
  if (ltActiveTraders[cand.mint]) {
    console.log("[AutoFeed] Skipping feed — already trading:", cand.mint);
    return;
  }
  try {
    console.info(`[AutoFeed] Feeding ${cand.mint} (${cand.symbol || "?"}) mcap=$${Math.round(cand.market_cap || 0)} into live trader…`);
    startLiveTrader(cand.mint);
  } catch (e) {
    console.error("[AutoFeed] Failed to start live trader for", cand.mint, e);
  }
}

function afRenderCandidates(list) {
  if (!Array.isArray(list) || list.length === 0) {
    afCandidates.innerHTML = `<div class="empty-state" style="font-size:11px;padding:14px">No candidates yet. Toggle AutoFeed on (requires wallet key).</div>`;
    return;
  }
  afCandidates.innerHTML = list.slice(0, 30).map(afRenderRowHTML).join("");
}

function afAddCandidateRow(cand) {
  // Prepend (removing empty state if present)
  const empty = afCandidates.querySelector(".empty-state");
  if (empty) empty.remove();
  const html = afRenderRowHTML(cand);
  afCandidates.insertAdjacentHTML("afterbegin", html);
  // Keep at most 30 rows
  const rows = afCandidates.querySelectorAll(".af-candidate");
  if (rows.length > 30) {
    rows[rows.length - 1].remove();
  }
}

function afRenderRowHTML(c) {
  const mint_short = (c.mint || "").slice(0, 8) + "…";
  const fmtUsd = (n) => {
    if (!n || n <= 0) return "—";
    if (n > 1e6) return `$${(n / 1e6).toFixed(2)}M`;
    if (n > 1e3) return `$${(n / 1e3).toFixed(1)}k`;
    return `$${n.toFixed(0)}`;
  };
  const fmtPct = (n) => (n != null && n > 0) ? (n * 100).toFixed(1) + "%" : "—";
  const fmtBool = (b, okWhenFalse = true) => {
    const cls = (b === false) ? (okWhenFalse ? "af-flag-ok" : "af-flag-bad") : "af-flag-bad";
    const lbl = (b === false) ? "✓" : "✗";
    return `<span class="${cls}">${lbl}</span>`;
  };
  return `
    <div class="af-candidate" data-mint="${c.mint}">
      <div>
        <span class="af-name">${c.symbol || mint_short}</span>
        <span class="af-sub"> / ${c.name || "—"}</span>
        <div class="af-sub">${c.mint}</div>
      </div>
      <div>
        <div class="af-sub">MCap</div>
        <div class="af-metric">${fmtUsd(c.market_cap)}</div>
        <div class="af-sub">Liq: ${fmtUsd(c.liquidity)}</div>
      </div>
      <div>
        <div class="af-sub">Smart / Holders</div>
        <div class="af-metric">${c.smart_degen_count || 0} / ${c.holders || 0}</div>
      </div>
      <div>
        <div class="af-sub">Rug / Bund</div>
        <div class="af-metric">${fmtPct(c.rug_ratio)} / ${fmtPct(c.bundler_rate)}</div>
      </div>
      <div>
        <div class="af-sub">Motion (vol / swaps)</div>
        <div class="af-metric" style="color:var(--green,#26a69a);font-weight:700">
          ${fmtUsd(c.volume)} / ${c.swaps || 0}
        </div>
        <div class="af-sub">1h: ${(c.price_change_1h != null && c.price_change_1h !== 0) ? ((c.price_change_1h >= 0 ? "+" : "") + Number(c.price_change_1h).toFixed(1) + "%") : "—"}</div>
      </div>
      <div>
        <div class="af-sub">Organic Gates</div>
        <div class="af-metric">
          Wash ${fmtBool(c.is_wash_trading)} ·
          RenMint ${fmtBool(c.renounced_mint, false)} ·
          RenFrz ${fmtBool(c.renounced_freeze, false)}
        </div>
        <button class="btn btn-xs btn-primary" style="margin-top:4px"
          onclick="afManualStart('${c.mint}')">⚡ Start</button>
      </div>
    </div>
  `;
}

function afManualStart(mint) {
  if (!ltWalletConnected) { alert("Connect wallet first."); return; }
  startLiveTrader(mint);
}
window.afManualStart = afManualStart;

/* ── Form read/write ───────────────────────────────────────────────── */
const AF_FIELD_MAP = [
  ["af-poll-seconds", "poll_seconds", "float"],
  ["af-interval", "interval", "str"],
  ["af-min-mcap", "min_mcap_usd", "float"],
  ["af-max-mcap", "max_mcap_usd", "float"],
  ["af-min-liq", "min_liquidity_usd", "float"],
  ["af-min-holders", "min_holders", "int"],
  ["af-min-smart", "min_smart_degen_count", "int"],
  ["af-min-volume", "min_volume_usd", "float"],
  ["af-min-swaps", "min_swaps", "int"],
  ["af-order-by", "order_by", "str"],
  ["af-migration-exchanges", "migration_exchanges", "str"],
  ["af-req-migration", "require_migration_exchange", "bool"],
  ["af-max-top10", "max_top10_holder_rate", "float"],
  ["af-max-rug", "max_rug_ratio", "float"],
  ["af-max-bundler", "max_bundler_rate", "float"],
  ["af-max-insider", "max_insider_rate", "float"],
  ["af-max-entrap", "max_entrapment_ratio", "float"],
  ["af-max-bot-degen", "max_bot_degen_rate", "float"],
  ["af-max-age", "max_created_age", "str"],
  ["af-platforms", "platforms", "str"],
  ["af-max-concurrent", "max_concurrent_feed", "int"],
  ["af-cooldown", "cooldown_after_feed_minutes", "float"],
  ["af-exclude", "exclude_mints", "str"],
  ["af-req-renounced-mint", "require_renounced_mint", "bool"],
  ["af-req-renounced-freeze", "require_renounced_freeze", "bool"],
  ["af-rej-wash", "reject_wash_trading", "bool"],
  ["af-rej-honeypot", "reject_honeypot", "bool"],
  ["af-req-social", "require_has_social", "bool"],
];

function afPopulateForm(snap) {
  for (const [id, key, type] of AF_FIELD_MAP) {
    const el = $(id);
    if (!el || snap[key] === undefined || snap[key] === null) continue;
    if (type === "bool") el.checked = !!snap[key];
    else el.value = snap[key];
  }
}

function afReadForm() {
  const out = {};
  for (const [id, key, type] of AF_FIELD_MAP) {
    const el = $(id);
    if (!el) continue;
    if (type === "bool") out[key] = !!el.checked;
    else if (type === "int") out[key] = parseInt(el.value, 10) || 0;
    else if (type === "float") out[key] = parseFloat(el.value) || 0;
    else out[key] = el.value.trim();
  }
  return out;
}

/* ── Button handlers ────────────────────────────────────────────────── */
afToggle.addEventListener("change", async () => {
  if (afToggle.checked) {
    if (!ltWalletConnected) {
      afToggle.checked = false;
      alert("⚠️  Cannot turn on autofeed without a private key set. Connect your wallet first.");
      afUpdateToggleGate();
      return;
    }
    // Tell backend the wallet-gate is satisfied (gate the autofeed loop)
    try {
      await apiFetch("/api/live/private_key", {
        method: "POST",
        body: JSON.stringify({ connected: true }),
      });
    } catch (e) { /* non-fatal; also reported to backend via /start below */ }
    // Send current config then start
    try {
      const cfg = afReadForm();
      await apiFetch("/api/autofeed/config", {
        method: "POST",
        body: JSON.stringify(cfg),
      });
      await apiFetch("/api/autofeed/start", { method: "POST", body: "{}" });
      afSettingsWrap.style.display = "block";
      afConnectWS();
    } catch (e) {
      afToggle.checked = false;
      console.error("[AutoFeed] start failed", e);
      alert("AutoFeed start failed: " + e.message);
    }
  } else {
    try {
      await apiFetch("/api/autofeed/stop", { method: "POST" });
      afSetStatus("off", "Stopped");
    } catch (e) { /* ignore */ }
    afDisconnectWS();
  }
});

afSaveConfigBtn.addEventListener("click", async () => {
  const cfg = afReadForm();
  try {
    const r = await apiFetch("/api/autofeed/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    });
    afConfigCache = r.snapshot || afConfigCache;
    afPreview.textContent = `Saved ${r.changed?.length || 0} field(s) ✓`;
    setTimeout(() => afPreview.textContent = "", 3000);
  } catch (e) {
    afPreview.textContent = "Save error: " + e.message;
    afPreview.style.color = "var(--red, #ef5350)";
  }
});

afTestPollBtn.addEventListener("click", async () => {
  if (!ltWalletConnected) return alert("Connect wallet first.");
  afTestPollBtn.disabled = true;
  afTestPollBtn.textContent = "Polling…";
  try {
    // Start then immediately stop — runs exactly one full poll since no candidate
    // will be processed safely.  Better: backend exposes "poll once" — we don't,
    // so fall back to fetch a status update.
    await apiFetch("/api/autofeed/config", {
      method: "POST",
      body: JSON.stringify(afReadForm()),
    });
    await apiFetch("/api/autofeed/start", { method: "POST", body: "{}" });
    await new Promise(r => setTimeout(r, 2500));   // allow 1 poll cycle
    await apiFetch("/api/autofeed/stop", { method: "POST" });
    const snap = await apiFetch("/api/autofeed/status");
    afHandleStatus(snap);
  } catch (e) {
    console.warn("[AutoFeed poll-once] failed", e);
  } finally {
    afTestPollBtn.disabled = false;
    afTestPollBtn.textContent = "🧪 Poll Once";
  }
});

/* ── Wire into wallet connect lifecycle ────────────────────────────── */
const _origConnectWallet = connectWallet;
connectWallet = function () {
  _origConnectWallet();
  if (ltWalletConnected) {
    afUpdateToggleGate();
    // Refresh backend status once WS is open
    setTimeout(async () => {
      try {
        const snap = await apiFetch("/api/autofeed/status");
        afHandleStatus(snap);
        afConnectWS();
      } catch (e) { /* ignore */ }
    }, 200);
  }
};
window.connectWallet = connectWallet;  // keep inline `onclick="connectWallet()"` working
ltConnectBtn.removeEventListener("click", _origConnectWallet);
ltConnectBtn.addEventListener("click", connectWallet);

/* ── Initial state ─────────────────────────────────────────────────── */
afUpdateToggleGate();
// Pull initial status from backend (in case autofeed is already running)
(async () => {
  try {
    const snap = await apiFetch("/api/autofeed/status");
    afHandleStatus(snap);
    if (snap.is_running) {
      afToggle.checked = true;
      afSettingsWrap.style.display = "block";
      afConnectWS();
    }
  } catch (e) { /* ignore until user navigates to page */ }
})();

// When wallet disconnects or page is left, gracefully shutdown
window.addEventListener("beforeunload", () => {
  try { afDisconnectWS(); } catch { /* ignore */ }
});

/* ══════════════════════════════════════════════════════════════════════════
   Market Data Module — fetch OHLCV data for Index Futures, Crypto, Equities
   ══════════════════════════════════════════════════════════════════════════ */

let mdSelectedAsset = null;   // currently selected asset key (e.g. "ES=F")
let mdCatalogue = [];         // full list from /api/market-data/catalogue
let mdFetching = false;

// Category → CSS class mapping
const MD_CATEGORY_CLASS = {
  "Index Futures": "futures",
  "Crypto Perpetuals": "crypto",
  "Equities": "equity",
};

// ── Per-category parameter presets ──────────────────────────────────────────
//
// Each preset is a full override of engineParamsV1 defaults tuned for the
// specific market microstructure.  Notes:
//
//  Index Futures (ES=F / NQ=F, 5-min bars):
