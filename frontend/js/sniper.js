/* ──────────────────────────────────────────────────────────────────────────
   Sniper Bot — Frontend Controller
   Connects to /api/sniper/* REST endpoints and /ws/sniper WebSocket.
   ────────────────────────────────────────────────────────────────────────── */

let sniperWs = null;
let sniperActive = false;
let sniperMode = "paper"; // "paper" | "live"
let sniperReconnectTimer = null;

/* ── DOM References ────────────────────────────────────────────────────── */

const elSniperStart     = document.getElementById("sniper-start-btn");
const elSniperStop      = document.getElementById("sniper-stop-btn");
const elSniperModeBtns  = document.querySelectorAll(".sniper-mode-btn");
const elSniperKeyRow    = document.getElementById("sniper-live-key-row");
const elSniperFeed      = document.getElementById("sniper-feed");
const elSniperPositions = document.getElementById("sniper-positions");
const elSniperTradesTable = document.getElementById("sniper-trades-tbody");

const statActive  = document.getElementById("sniper-status-active");
const statTrades  = document.getElementById("sniper-status-trades");
const statPnl     = document.getElementById("sniper-status-pnl");
const statWinRate = document.getElementById("sniper-status-winrate");
const statMode    = document.getElementById("sniper-status-mode");

/* ── Mode Toggle ───────────────────────────────────────────────────────── */

elSniperModeBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    if (sniperActive) {
      alert("Cannot change mode while Sniper is active.");
      return;
    }
    elSniperModeBtns.forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    sniperMode = btn.dataset.mode;
    if (statMode) statMode.textContent = sniperMode;
    if (elSniperKeyRow) {
      elSniperKeyRow.style.display = sniperMode === "live" ? "block" : "none";
    }
  });
});

/* ── Start / Stop ──────────────────────────────────────────────────────── */

if (elSniperStart) {
  elSniperStart.addEventListener("click", async () => {
    // Set mode first
    const modeBody = sniperMode === "live" ? "live" : "forward_test";
    try {
      const modeResp = await fetch("/api/sniper/mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: modeBody }),
      });
      const modeResult = await modeResp.json();
      if (modeResult.error) {
        alert(`Cannot switch mode: ${modeResult.error}`);
        return;
      }
    } catch (e) {
      console.error("[Sniper] Mode set failed:", e);
    }

    // Start the engine
    try {
      await fetch("/api/sniper/start", { method: "POST" });
    } catch (e) {
      console.error("[Sniper] Start failed:", e);
      return;
    }

    sniperActive = true;
    elSniperStart.style.display = "none";
    elSniperStop.style.display = "inline-flex";
    const pulse = document.getElementById("sniper-pulse");
    if (pulse) pulse.style.display = "inline-block";

    // Show status bar
    const bar = document.getElementById("sniper-status-bar");
    if (bar) bar.classList.remove("hidden");

    connectSniperWs();
  });
}

if (elSniperStop) {
  elSniperStop.addEventListener("click", async () => {
    sniperActive = false;
    elSniperStart.style.display = "inline-flex";
    elSniperStop.style.display = "none";
    const pulse = document.getElementById("sniper-pulse");
    if (pulse) pulse.style.display = "none";

    try {
      await fetch("/api/sniper/stop", { method: "POST" });
    } catch (e) {
      console.error("[Sniper] Stop failed:", e);
    }

    if (sniperWs) { sniperWs.close(); sniperWs = null; }
    if (sniperReconnectTimer) { clearTimeout(sniperReconnectTimer); sniperReconnectTimer = null; }
  });
}

/* ── WebSocket Connection ──────────────────────────────────────────────── */

function connectSniperWs() {
  if (sniperWs && sniperWs.readyState <= 1) return; // already open/connecting

  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  sniperWs = new WebSocket(`${wsProto}//${location.host}/ws/sniper`);

  sniperWs.onopen = () => {
    console.log("[SniperWS] Connected");
  };

  sniperWs.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleSniperEvent(msg);
  };

  sniperWs.onclose = () => {
    console.log("[SniperWS] Disconnected");
    if (sniperActive) {
      sniperReconnectTimer = setTimeout(connectSniperWs, 2000);
    }
  };

  sniperWs.onerror = (e) => {
    console.error("[SniperWS] Error:", e);
  };
}

/* ── Event Router ──────────────────────────────────────────────────────── */

function handleSniperEvent(msg) {
  switch (msg.type) {
    case "status":
      updateSniperStatus(msg);
      break;
    case "token_detected":
      addDetectedCard(msg);
      break;
    case "token_watching":
      addWatchingCard(msg);
      break;
    case "entry":
      moveCardToActive(msg);
      break;
    case "exit":
      removeActiveCard(msg);
      logSniperTrade(msg);
      break;
    case "filter_rejected":
      markCardRejected(msg);
      break;
    case "ping":
      if (sniperWs) sniperWs.send(JSON.stringify({ type: "pong" }));
      break;
  }
}

/* ── Status Update ─────────────────────────────────────────────────────── */

function updateSniperStatus(msg) {
  if (statActive) statActive.textContent = msg.watching_count || 0;
  if (statTrades) statTrades.textContent = msg.forward_test_stats?.total_trades || 0;
  if (statMode) statMode.textContent = msg.mode || "forward_test";
  if (msg.forward_test_stats) {
    const stats = msg.forward_test_stats;
    if (statWinRate) statWinRate.textContent = (stats.win_rate || 0).toFixed(1) + "%";
    if (statPnl) {
      const pnl = stats.total_net_pnl_sol || 0;
      statPnl.textContent = (pnl >= 0 ? "+" : "") + pnl.toFixed(4) + " SOL";
      statPnl.style.color = pnl >= 0 ? "var(--green)" : "var(--down)";
    }
  }
}

/* ── Token Cards ───────────────────────────────────────────────────────── */

function addDetectedCard(msg) {
  // Cap radar at 80 cards
  const cards = elSniperFeed.querySelectorAll(".sniper-radar-card");
  if (cards.length >= 80) cards[cards.length - 1].remove();

  if (elSniperFeed.querySelector(".empty-state")) elSniperFeed.innerHTML = "";

  const label = msg.name ? `${msg.name} (${msg.symbol})` : `${msg.mint.slice(0,6)}…${msg.mint.slice(-4)}`;

  const div = document.createElement("div");
  div.className = "backtest-card sniper-radar-card";
  div.id = "feed-card-" + msg.mint;
  div.style.cssText = "padding:10px 14px; animation: fadeIn 0.3s ease; opacity:0.5;";
  div.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <span style="font-weight:700; font-family:var(--mono); color:#fff; font-size:13px;">${label}</span>
      <span class="rec-card-badge" style="background:var(--surface); color:var(--muted); border:1px solid var(--border); font-size:10px;">👁 Detected</span>
    </div>
    <div style="font-size:11px; color:var(--muted); margin-top:5px; display:flex; gap:12px;">
      <span>MC: ${(msg.genesis_mc || 0).toFixed(1)} SOL</span>
      <span style="color:var(--muted)">Fees: calculating…</span>
    </div>
  `;
  elSniperFeed.prepend(div);
}

function addWatchingCard(msg) {
  // Upgrade existing detected card or create new one
  const existing = document.getElementById("feed-card-" + msg.mint);
  if (existing) {
    existing.style.opacity = "1";
    existing.style.borderColor = "var(--accent)";
    const badge = existing.querySelector(".rec-card-badge");
    if (badge) {
      badge.style.background = "rgba(88,101,242,0.12)";
      badge.style.color = "var(--accent)";
      badge.style.borderColor = "rgba(88,101,242,0.3)";
      badge.innerHTML = "📡 Watching";
    }
    // Update metrics
    const metricsDiv = existing.querySelector("div:last-child");
    if (metricsDiv) {
      const dipPct = ((msg.dip_depth || 0) * 100).toFixed(1);
      metricsDiv.innerHTML = `
        <span style="color:var(--down)">▼ ${dipPct}% dip</span>
        <span>Fees: ${(msg.fees_sol || 0).toFixed(4)} SOL</span>
      `;
    }
    return;
  }

  if (elSniperFeed.querySelector(".empty-state")) elSniperFeed.innerHTML = "";

  const dipPct = ((msg.dip_depth || 0) * 100).toFixed(1);
  const div = document.createElement("div");
  div.className = "backtest-card sniper-radar-card";
  div.id = "feed-card-" + msg.mint;
  div.style.cssText = "padding:10px 14px; animation: fadeIn 0.3s ease;";
  div.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <span style="font-weight:700; font-family:var(--mono); color:#fff; font-size:13px;">${msg.mint.slice(0,6)}…${msg.mint.slice(-4)}</span>
      <span class="rec-card-badge" style="background:rgba(88,101,242,0.12); color:var(--accent); border:1px solid rgba(88,101,242,0.3); font-size:10px;">📡 Watching</span>
    </div>
    <div style="font-size:11px; color:var(--muted); margin-top:5px; display:flex; gap:12px;">
      <span style="color:var(--down)">▼ ${dipPct}% dip</span>
      <span>Fees: ${(msg.fees_sol || 0).toFixed(4)} SOL</span>
    </div>
  `;
  elSniperFeed.prepend(div);
}

function markCardRejected(msg) {
  const existing = document.getElementById("feed-card-" + msg.mint);
  if (existing) {
    existing.style.opacity = "0.3";
    existing.style.borderColor = "var(--down)";
    const badge = existing.querySelector(".rec-card-badge");
    if (badge) {
      badge.style.background = "rgba(239,83,80,0.12)";
      badge.style.color = "var(--down)";
      badge.innerHTML = "✕ Rejected";
    }
  }
}

function moveCardToActive(msg) {
  const existing = document.getElementById("feed-card-" + msg.mint);
  if (existing) existing.remove();

  if (elSniperPositions.querySelector(".empty-state")) elSniperPositions.innerHTML = "";

  const convColor = msg.conviction === "high" ? "var(--green)"
    : msg.conviction === "medium" ? "#f5a623" : "var(--accent)";

  const div = document.createElement("div");
  div.className = "backtest-card";
  div.id = "active-card-" + msg.mint;
  div.style.borderColor = "var(--green)";
  div.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <span style="font-weight:700; font-family:var(--mono); color:#fff;">${msg.mint.slice(0,6)}…${msg.mint.slice(-4)}</span>
      <span class="rec-card-badge" style="background:rgba(99,217,94,0.1); color:var(--green); border:1px solid rgba(99,217,94,0.2);">
        <span class="lt-live-dot" style="background:var(--green); margin-right:4px;"></span> Active
      </span>
    </div>
    <div style="font-size:11px; color:var(--muted);">
      Entry MC: <b style="color:#fff;">${(msg.entry_mc || 0).toFixed(2)}</b> &bull;
      Size: <b style="color:var(--text);">${msg.size_sol} SOL</b> &bull;
      <span style="color:${convColor}; font-weight:700; text-transform:uppercase;">${msg.conviction}</span>
    </div>
    <div style="font-size:11px; color:var(--muted); margin-top:6px;">
      Conditions: ${formatConditions(msg.conditions)}
    </div>
  `;
  elSniperPositions.prepend(div);

  // Update active count
  const curr = parseInt(statActive?.textContent) || 0;
  if (statActive) statActive.textContent = curr + 1;
}

function formatConditions(conditions) {
  if (!conditions) return "—";
  return Object.entries(conditions)
    .map(([k, v]) => {
      const label = k.replace("c", "").replace("_", ": ");
      const icon = v ? "✅" : "❌";
      return `<span style="color:${v ? 'var(--green)' : 'var(--down)'}; margin-right:4px;">${icon}${label}</span>`;
    })
    .join("");
}

function removeActiveCard(msg) {
  const existing = document.getElementById("active-card-" + msg.mint);
  if (existing) existing.remove();

  // Restore empty state if no more active positions
  if (elSniperPositions && !elSniperPositions.querySelector(".backtest-card")) {
    elSniperPositions.innerHTML = '<div class="empty-state">No active positions</div>';
  }

  const curr = parseInt(statActive?.textContent) || 0;
  if (statActive) statActive.textContent = Math.max(0, curr - 1);
}

/* ── Trade History ─────────────────────────────────────────────────────── */

let sniperTradeCount = 0;

function logSniperTrade(msg) {
  sniperTradeCount++;
  const pnlSol = msg.pnl_sol || 0;
  const pnlPct = msg.pnl_pct || 0;
  const pnlColor = pnlSol >= 0 ? "var(--up)" : "var(--down)";
  const sign = pnlSol >= 0 ? "+" : "";

  const emptyState = document.getElementById("sniper-history-empty");
  if (emptyState) emptyState.remove();

  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td style="color:var(--muted);">${sniperTradeCount}</td>
    <td style="font-weight:600; font-family:var(--mono);">${msg.mint.slice(0,6)}…${msg.mint.slice(-4)}</td>
    <td><span class="rec-card-badge" style="background:var(--surface); color:var(--text);">${sniperMode === "live" ? "⚡ Live" : "📋 Paper"}</span></td>
    <td><span style="color:var(--green);">Closed</span></td>
    <td>Sell</td>
    <td style="color:${pnlColor}; font-weight:700;">${sign}${pnlSol.toFixed(4)} SOL</td>
    <td style="color:${pnlColor};">${sign}${pnlPct.toFixed(2)}%</td>
    <td style="color:var(--muted);">—</td>
    <td><span style="color:var(--accent);">${msg.trigger || "—"}</span></td>
  `;
  elSniperTradesTable.prepend(tr);

  // Update session stats
  if (statTrades) statTrades.textContent = sniperTradeCount;

  // Refresh full stats from server
  fetchAndUpdateStats();
}

/* ── Stats Refresh ─────────────────────────────────────────────────────── */

async function fetchAndUpdateStats() {
  try {
    const resp = await fetch("/api/sniper/forward-test/stats");
    if (!resp.ok) return;
    const stats = await resp.json();

    if (statTrades) statTrades.textContent = stats.total_trades || 0;
    if (statWinRate) statWinRate.textContent = (stats.win_rate || 0).toFixed(1) + "%";
    if (statPnl) {
      const pnl = stats.total_net_pnl_sol || 0;
      statPnl.textContent = (pnl >= 0 ? "+" : "") + pnl.toFixed(4) + " SOL";
      statPnl.style.color = pnl >= 0 ? "var(--green)" : "var(--down)";
    }
  } catch (e) {
    console.error("[Sniper] Stats fetch failed:", e);
  }
}

/* ── Load initial stats on page load ────────────────────────────────────── */

document.addEventListener("DOMContentLoaded", () => {
  // Fetch stats on initial load (even if sniper isn't running)
  fetchAndUpdateStats();
});
