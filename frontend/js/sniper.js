// frontend/js/sniper.js

let sniperWs = null;
let sniperActive = false;
let sniperMode = "paper"; // or "live"

const elSniperStart  = document.getElementById("sniper-start-btn");
const elSniperStop   = document.getElementById("sniper-stop-btn");
const elSniperModeBtns = document.querySelectorAll(".sniper-mode-btn");
const elSniperKeyRow = document.getElementById("sniper-live-key-row");

const elSniperFeed      = document.getElementById("sniper-feed");
const elSniperPositions = document.getElementById("sniper-positions");
const elSniperTradesTable = document.getElementById("sniper-trades-tbody");

const statActive   = document.getElementById("sniper-status-active");
const statTrades   = document.getElementById("sniper-status-trades");
const statPnl      = document.getElementById("sniper-status-pnl");
const statWinRate  = document.getElementById("sniper-status-winrate");

// Mode Toggle
elSniperModeBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    if (sniperActive) { alert("Cannot change mode while Sniper is active."); return; }
    elSniperModeBtns.forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    sniperMode = btn.dataset.mode;
    document.getElementById("sniper-status-mode").textContent = sniperMode;
    elSniperKeyRow.style.display = sniperMode === "live" ? "block" : "none";
  });
});

// START
if (elSniperStart) {
  elSniperStart.addEventListener("click", () => {
    sniperActive = true;
    elSniperStart.style.display = "none";
    elSniperStop.style.display  = "inline-flex";
    const pulse = document.getElementById("sniper-pulse");
    if (pulse) pulse.style.display = "inline-block";

    // Show status bar
    const bar = document.getElementById("sniper-status-bar");
    if (bar) bar.classList.remove("hidden");

    const priorityFee = parseFloat(document.getElementById("sniper-priority-fee")?.value) || 0.0001;

    fetch("/sniper/start", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ mode: sniperMode, priority_fee_sol: priorityFee })
    });

    connectSniperWs();
  });
}

// STOP
if (elSniperStop) {
  elSniperStop.addEventListener("click", () => {
    sniperActive = false;
    elSniperStart.style.display = "inline-flex";
    elSniperStop.style.display  = "none";
    const pulse = document.getElementById("sniper-pulse");
    if (pulse) pulse.style.display = "none";

    fetch("/sniper/stop", { method: "POST" });
    if (sniperWs) { sniperWs.close(); sniperWs = null; }
  });
}

function connectSniperWs() {
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  sniperWs = new WebSocket(`${wsProto}//${location.host}/sniper/ws`);

  sniperWs.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }

    if (msg.type === "token_watching") {
      addTokenCard(msg);
    } else if (msg.type === "entry") {
      moveCardToActive(msg);
    } else if (msg.type === "exit") {
      removeActiveCard(msg);
      logTrade(msg);
      updateSessionStats(msg);
    }
  };

  sniperWs.onclose = () => {
    if (sniperActive) setTimeout(connectSniperWs, 2000);
  };
}

function addTokenCard(msg) {
  // Cap radar at 50 cards — remove oldest if over limit
  const cards = elSniperFeed.querySelectorAll(".sniper-radar-card");
  if (cards.length >= 50) cards[cards.length - 1].remove();

  if (elSniperFeed.querySelector(".empty-state")) elSniperFeed.innerHTML = "";

  const dipLabel = msg.dip_pct > 0
    ? `<span style="color:var(--down)">▼ ${msg.dip_pct}% dip</span>`
    : `<span style="color:var(--muted)">Watching…</span>`;

  const div = document.createElement("div");
  div.className = "backtest-card sniper-radar-card";
  div.id = "feed-card-" + msg.mint;
  div.style.cssText = "padding:10px 14px; animation: fadeIn 0.3s ease;";
  div.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <span style="font-weight:700; font-family:var(--mono); color:#fff; font-size:13px;">${msg.mint.slice(0,6)}…${msg.mint.slice(-4)}</span>
      <span class="rec-card-badge" style="background:var(--surface); color:var(--accent); border:1px solid var(--border); font-size:10px;">📡 Tracking</span>
    </div>
    <div style="font-size:11px; color:var(--muted); margin-top:5px; display:flex; gap:12px;">
      ${dipLabel}
      <span>Fees: ${(msg.fees_sol || 0).toFixed(4)} SOL</span>
    </div>
  `;
  elSniperFeed.prepend(div);
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
    <div style="font-size:10px; color:var(--muted); margin-top:4px;" id="pnl-${msg.mint}">P&amp;L: watching…</div>
  `;
  elSniperPositions.prepend(div);

  // Update active count
  const curr = parseInt(statActive.textContent) || 0;
  statActive.textContent = curr + 1;
}

function removeActiveCard(msg) {
  const existing = document.getElementById("active-card-" + msg.mint);
  if (existing) existing.remove();
  const curr = parseInt(statActive.textContent) || 0;
  statActive.textContent = Math.max(0, curr - 1);
}

function updateSessionStats(msg) {
  // Backend sends session totals
  if (msg.total_trades !== undefined) statTrades.textContent = msg.total_trades;
  if (msg.win_rate !== undefined) statWinRate.textContent = (msg.win_rate * 100).toFixed(1) + "%";
  if (msg.session_pnl !== undefined) {
    const pnl = msg.session_pnl;
    statPnl.textContent = (pnl >= 0 ? "+" : "") + pnl.toFixed(4) + " SOL";
    statPnl.style.color = pnl >= 0 ? "var(--green)" : "var(--down)";
  }
}

let totalTradesCount = 0;

function logTrade(msg) {
  totalTradesCount++;
  const pnlSol = msg.pnl_sol || 0;
  const pnlPct = msg.pnl_pct || 0;
  const pnlColor = pnlSol >= 0 ? "var(--up)" : "var(--down)";
  const sign     = pnlSol >= 0 ? "+" : "";

  const emptyState = document.getElementById("sniper-history-empty");
  if (emptyState) emptyState.remove();

  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td style="color:var(--muted);">${totalTradesCount}</td>
    <td style="font-weight:600; font-family:var(--mono);">${msg.mint.slice(0,6)}…${msg.mint.slice(-4)}</td>
    <td><span class="rec-card-badge" style="background:var(--surface); color:var(--text);">${sniperMode}</span></td>
    <td><span style="color:var(--green);">Closed</span></td>
    <td>Sell</td>
    <td style="color:${pnlColor}; font-weight:700;">${sign}${pnlSol.toFixed(4)} SOL</td>
    <td style="color:${pnlColor};">${sign}${(pnlPct * 100).toFixed(2)}%</td>
    <td style="color:var(--muted);">—</td>
    <td><span style="color:var(--accent);">${msg.trigger}</span></td>
  `;
  elSniperTradesTable.prepend(tr);
}
