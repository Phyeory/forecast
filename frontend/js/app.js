/* ──────────────────────────────────────────────────────────────────────────
   pump-chart  ·  Forward-Testing UI
   ────────────────────────────────────────────────────────────────────────── */

const WS_BASE      = `ws://${location.host}/ws`;
const MAX_TRADES   = 60;
const RECONNECT_MS = 1500;
const CANDLE_UP    = "#26a69a";
const CANDLE_DOWN  = "#ef5350";
const CANDLE_FLAT  = "#5a6071";

let chart, candleSeries, volSeries, ema3Series, ema7Series, ws, reconnectTimer;
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

// Strategy state
let markers = [];
let lastStrategy = null;

const $ = id => document.getElementById(id);
const mintInput   = $("mint-input");
const loadBtn     = $("load-btn");
const tfBtns      = document.querySelectorAll(".tf-btn");
const dot         = $("dot");
const connLabel   = $("conn-label");
const tokenBar    = $("token-bar");
const tokenLogo   = $("token-logo");
const tokenName   = $("token-name");
const tokenSymbol = $("token-symbol");
const lastPriceEl = $("last-price");
const priceChange = $("price-change");
const mcapEl      = $("stat-mcap");
const volEl       = $("stat-vol");
const ohlcOpenEl  = $("ohlc-open");
const ohlcHighEl  = $("ohlc-high");
const ohlcLowEl   = $("ohlc-low");
const ohlcCloseEl = $("ohlc-close");
const tradeFeed   = $("trade-feed");
const overlay     = $("overlay");
const overlayIcon = $("overlay-icon");
const overlayMsg  = $("overlay-msg");

// Strategy dashboard elements
const strategyBar   = $("strategy-bar");
const stratRegime   = $("strat-regime");
const stratDir      = $("strat-direction");
const stratSignalS  = $("strat-signal-s");
const stratRoc      = $("strat-roc");
const stratAtr      = $("strat-atr");
const stratPosition = $("strat-position");
const stratBalance  = $("strat-balance");
const stratPnl      = $("strat-pnl");
const stratTrades   = $("strat-trades");
const stratWinrate  = $("strat-winrate");
const stratFees     = $("strat-fees");
const stratSlippage = $("strat-slippage");
const vpOverlay     = $("volume-profile-overlay");

/* ── Chart init ──────────────────────────────────────────────────────── */

function initChart() {
  const wrapper = $("chart");
  if (chart) chart.remove();
  chart = LightweightCharts.createChart(wrapper, {
    layout:      { background: { color: "#0d0f12" }, textColor: "#5a6071" },
    grid:        { vertLines: { color: "#1e2330" }, horzLines: { color: "#1e2330" } },
    crosshair:   { mode: LightweightCharts.CrosshairMode.Normal, vertLine: { color: "#5865f2", labelBackgroundColor: "#5865f2" }, horzLine: { color: "#5865f2", labelBackgroundColor: "#5865f2" } },
    timeScale:   { borderColor: "#1e2330", timeVisible: true, secondsVisible: true, rightBarStaysOnScroll: true, shiftVisibleRangeOnNewBar: true },
    rightPriceScale: { borderColor: "#1e2330", scaleMargins: { top: 0.12, bottom: 0.12 } },
    handleScroll: { mouseWheel: true, pressedMouseMove: true },
    handleScale:  { mouseWheel: true, pinch: true },
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

  // EMA overlays
  ema3Series = chart.addLineSeries({
    color: "#00e5ff",
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Solid,
    priceFormat: { type: "custom", minMove: 1, formatter: v => formatMcap(v) },
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false,
  });
  ema7Series = chart.addLineSeries({
    color: "#ff9800",
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Solid,
    priceFormat: { type: "custom", minMove: 1, formatter: v => formatMcap(v) },
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false,
  });

  volSeries = chart.addHistogramSeries({
    color: "#5865f222", priceFormat: { type: "volume" }, priceScaleId: "volume",
  });
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

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
  if (n >= 1_000_000)     return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000)         return (n / 1_000).toFixed(1) + "K";
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
    for (let t = lastCandleTime + tfSeconds; t < candle.time; t += tfSeconds) {
      const flat = { time: t, open: lastCandleClose, high: lastCandleClose, low: lastCandleClose, close: lastCandleClose, volume: 0 };
      candleSeries.update(withCandleColors(flat, lastCandleClose));
      updateOhlc(flat);
      lastCandleTime = t;
      lastCandleClose = flat.close;
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
  overlayMsg.textContent  = msg;
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
  const tm = document.createElement("span"); tm.className = "trade-time"; tm.textContent = new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
  row.append(t, s, tm);
  tradeFeed.prepend(row);
  while (tradeFeed.children.length > MAX_TRADES) tradeFeed.lastElementChild.remove();
}

/* ── Strategy Dashboard Updates ──────────────────────────────────────── */

const REGIME_CLASSES = {
  IDLE:         "regime-badge idle",
  TREND:        "regime-badge trend",
  EXHAUSTION:   "regime-badge exhaustion",
  REVERSAL:     "regime-badge reversal",
  CONTINUATION: "regime-badge continuation",
};

function updateStrategyDashboard(strat, simState, simTrade) {
  if (!strat) return;
  strategyBar.classList.remove("hidden");

  // Regime badge
  const regime = strat.regime || "IDLE";
  stratRegime.textContent = regime;
  stratRegime.className = REGIME_CLASSES[regime] || REGIME_CLASSES.IDLE;

  // Direction
  const dir = strat.regime_direction || "—";
  stratDir.textContent = dir === "UP" ? "▲ UP" : dir === "DOWN" ? "▼ DOWN" : "—";
  stratDir.className = "strat-dir " + (dir === "UP" ? "up" : dir === "DOWN" ? "down" : "");

  // Indicator values
  const s = strat.signal_s ?? 0;
  stratSignalS.textContent = s.toFixed(2);
  stratSignalS.className = "strat-value " + (s > 1.5 ? "strong" : s > 1 ? "moderate" : s > 0.8 ? "weak" : "noise");

  stratRoc.textContent = ((strat.roc || 0) * 100).toFixed(2) + "%";
  stratRoc.className = "strat-value " + ((strat.roc || 0) >= 0 ? "pos" : "neg");

  const atrVal = strat.atr || 0;
  stratAtr.textContent = atrVal > 0.001 ? atrVal.toFixed(6) : atrVal.toExponential(2);

  // Sim state
  if (simState) {
    const pos = simState.position;
    if (pos) {
      stratPosition.textContent = `LONG @ ${pos.entry_price > 0.001 ? pos.entry_price.toFixed(6) : pos.entry_price.toExponential(2)}`;
      stratPosition.className = "strat-value pos";
    } else {
      stratPosition.textContent = "None";
      stratPosition.className = "strat-value dim";
    }
    stratBalance.textContent = simState.balance.toFixed(4) + " SOL";
    const pnl = simState.total_pnl || 0;
    stratPnl.textContent = (pnl >= 0 ? "+" : "") + pnl.toFixed(4) + " SOL";
    stratPnl.className = "strat-value " + (pnl >= 0 ? "pos" : "neg");
    stratTrades.textContent = simState.trade_count || 0;
    stratWinrate.textContent = simState.trade_count > 0 ? (simState.win_rate * 100).toFixed(0) + "%" : "—";
    stratFees.textContent = (simState.total_fees || 0).toFixed(5) + " SOL";
    stratSlippage.textContent = (simState.total_slippage_cost || 0).toFixed(4) + " SOL";
  }

  lastStrategy = strat;
}

/* ── EMA Line Updates ────────────────────────────────────────────────── */

function updateEmaLines(time, strat) {
  if (!strat || !time) return;
  if (strat.warming_up) return;

  const ema3mcap = toMarketCapValue(strat.ema3);
  const ema7mcap = toMarketCapValue(strat.ema7);

  if (ema3mcap > 0) ema3Series.update({ time, value: ema3mcap });
  if (ema7mcap > 0) ema7Series.update({ time, value: ema7mcap });
}

/* ── Buy/Sell Markers ────────────────────────────────────────────────── */

function addSignalMarker(time, type, priceMcap) {
  if (!time) return;
  const isBuy = type === "BUY";
  markers.push({
    time,
    position: isBuy ? "belowBar" : "aboveBar",
    color: isBuy ? "#00e676" : "#ff1744",
    shape: isBuy ? "arrowUp" : "arrowDown",
    text: isBuy ? "BUY" : "SELL",
    size: 2,
  });
  // Sort markers by time (required by lightweight-charts)
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
}

/* ── Volume Profile Overlay ──────────────────────────────────────────── */

function renderVolumeProfile(vp, chartEl) {
  if (!vp || !vp.bins || !vp.bins.length) {
    vpOverlay.innerHTML = "";
    return;
  }

  const maxVol = Math.max(...vp.bins.map(b => b.total));
  if (maxVol <= 0) { vpOverlay.innerHTML = ""; return; }

  const chartHeight = chartEl.clientHeight;
  const prices = vp.bins.map(b => b.price);
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  const range = maxP - minP || 1;

  // Build bars
  let html = "";
  const barMaxWidth = 60; // px
  const barHeight = Math.max(2, Math.floor(chartHeight * 0.7 / vp.bins.length));

  for (const bin of vp.bins) {
    const pct = bin.total / maxVol;
    const width = Math.max(2, pct * barMaxWidth);
    const yPct = 1 - (bin.price - minP) / range;
    const top = 12 + yPct * (chartHeight * 0.7);
    const delta = bin.delta || 0;
    const color = delta > 0
      ? `rgba(38,166,154,${0.3 + pct * 0.5})`
      : `rgba(239,83,80,${0.3 + pct * 0.5})`;
    const isPoc = Math.abs(bin.price - vp.poc) < range / vp.bins.length;
    const border = isPoc ? "border-right:2px solid #ffd740;" : "";

    html += `<div class="vp-bar" style="top:${top}px;width:${width}px;height:${barHeight}px;background:${color};${border}" title="Price: ${bin.price.toExponential(3)} Vol: ${bin.total.toFixed(3)}"></div>`;
  }

  // Value area lines
  if (vp.value_area_low > 0 && vp.value_area_high > 0) {
    const vaLowY = 12 + (1 - (vp.value_area_low - minP) / range) * (chartHeight * 0.7);
    const vaHighY = 12 + (1 - (vp.value_area_high - minP) / range) * (chartHeight * 0.7);
    html += `<div class="vp-va-line" style="top:${vaHighY}px"></div>`;
    html += `<div class="vp-va-line" style="top:${vaLowY}px"></div>`;
  }

  vpOverlay.innerHTML = html;
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
  markers = [];
  lastStrategy = null;

  tokenBar.classList.add("hidden");
  strategyBar.classList.add("hidden");
  tradeFeed.innerHTML = "";
  vpOverlay.innerHTML = "";
  if (candleSeries) { candleSeries.setData([]); candleSeries.setMarkers([]); }
  if (volSeries)    volSeries.setData([]);
  if (ema3Series)   ema3Series.setData([]);
  if (ema7Series)   ema7Series.setData([]);
  showOverlay("⏳", "Connecting…");
  setDot("connecting", "Connecting…");

  ws = new WebSocket(`${WS_BASE}/${mint}?timeframe=${timeframe}`);

  ws.onopen = () => {
    reconnectMs = RECONNECT_MS;
    setDot("connected", "Live");
    showOverlay("📡", "Waiting for data…");
  };

  ws.onmessage = ev => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.type === "token_info") {
      const d = msg.data;
      tokenName.textContent   = d.name   || mint.slice(0, 8) + "…";
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
        { key: "twitter",  label: "𝕏",  base: "https://twitter.com/" },
        { key: "telegram", label: "TG", base: "" },
        { key: "website",  label: "🌐", base: "" },
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
      strategyBar.classList.remove("hidden");
    }

    else if (msg.type === "historical") {
      const cs = msg.candles;
      if (!cs || !cs.length) { showOverlay("🔍", "No historical data. Waiting for live trades…"); return; }
      if (LIVE_ONLY_MARKETCAP) {
        if (cs[cs.length - 1]?.close > 0) chartBasePrice = cs[cs.length - 1].close;
        showOverlay("📡", "Live market-cap mode. Waiting for ticks…");
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

      // ── Strategy overlays ──────────────────────────────────────────
      if (msg.strategy) {
        updateStrategyDashboard(msg.strategy, msg.sim_state, msg.sim_trade);
        updateEmaLines(msg.candle?.time, msg.strategy);

        // Volume profile
        if (msg.strategy.volume_profile) {
          renderVolumeProfile(msg.strategy.volume_profile, $("chart"));
        }

        // Buy/Sell markers from sim trade
        if (msg.sim_trade && !msg.sim_trade.error) {
          addSignalMarker(
            msg.candle?.time,
            msg.sim_trade.action,
            c.close
          );

          // Sim trade notification in trade feed
          const stRow = document.createElement("div");
          stRow.className = `trade-row sim-trade ${msg.sim_trade.action === "BUY" ? "buy" : "sell"}`;
          const label = document.createElement("span");
          label.className = `trade-type ${msg.sim_trade.action === "BUY" ? "buy" : "sell"}`;
          label.textContent = "SIM " + msg.sim_trade.action;
          const info = document.createElement("span");
          info.className = "trade-sol";
          if (msg.sim_trade.action === "BUY") {
            info.textContent = (msg.sim_trade.amount_sol || 0).toFixed(3) + " SOL";
          } else {
            info.textContent = (msg.sim_trade.pnl >= 0 ? "+" : "") + (msg.sim_trade.pnl || 0).toFixed(4) + " SOL";
          }
          const stTime = document.createElement("span");
          stTime.className = "trade-time";
          stTime.textContent = new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
          stRow.append(label, info, stTime);
          tradeFeed.prepend(stRow);
        }
      }

      chart.timeScale().scrollToRealTime();
      hideOverlay();
    }

    else if (msg.type === "error") showOverlay("⚠️", msg.message || "Error");
    else if (msg.type === "ping") ws.send(JSON.stringify({type:"pong"}));
  };

  ws.onerror = () => { setDot("error", "Error"); showOverlay("❌", "Connection error. Reconnecting…"); };
  ws.onclose = () => {
    setDot("error", "Disconnected");
    showOverlay("🔌", `Disconnected — reconnecting in ${reconnectMs/1000}s…`);
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

initChart();
