/* ──────────────────────────────────────────────────────────────────────────
   pump-chart  ·  Price Action Streaming Dashboard
   ────────────────────────────────────────────────────────────────────────── */

const WS_BASE      = `ws://${location.host}/ws`;
const MAX_TRADES   = 60;
const RECONNECT_MS = 1500;
const CANDLE_UP    = "#26a69a";
const CANDLE_DOWN  = "#ef5350";
const CANDLE_FLAT  = "#5a6071";

let chart, candleSeries, volSeries, ws, reconnectTimer;
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
  if (candleSeries) { candleSeries.setData([]); }
  if (volSeries)    volSeries.setData([]);
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
