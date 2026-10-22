#!/usr/bin/env bash
# Daily prop-sim step: resume the account, decide every bar that closed since
# the last run, book the fills, publish the dashboard snapshot, persist state.
# Runs on GitHub Actions (free, no card) and is idempotent — a second run
# with nothing new does nothing.
#
#   repo root/
#     backend/            the market_live runtime
#     deploy/run_daily.sh this script
#     runtime/            state.json, account.json, equity.json, trades.json,
#                         guard_state.json, snap.pkl.gz, journal/, paused,
#                         armed.json {"mode": paper|real|off}
#
# Modes (runtime/armed.json — written by the dashboard control plane):
#   paper  resume-based FTMO-2-step paper sim (paper broker, no bridge)
#   real   LIVE FTMO MT5 account through an SSH tunnel to the Windows VPS
#          bridge (secrets PROP_VPS_HOST / PROP_VPS_USER / PROP_VPS_SSH_KEY)
#   off    no new decisions (report-only refresh)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY=python3
export PYTHONPATH="$ROOT/backend"

$PY -m pip install --quiet --disable-pip-version-check numpy numba scipy || \
  $PY -m pip install --quiet --disable-pip-version-check numpy scipy

echo "[prop-sim] $(date -u +%FT%TZ) — daily step (cwd=$ROOT)"

if [ -f runtime/paused ]; then
  echo "[prop-sim] PAUSED — no new decisions; refreshing the report only"
  $PY -m market_live.remote_report --root "$ROOT"
  exit 0
fi

MODE=paper
if [ -f runtime/armed.json ]; then
  MODE=$($PY -c 'import json; print(json.load(open("runtime/armed.json")).get("mode", "paper"))' 2>/dev/null || echo paper)
fi
echo "[prop-sim] armed mode: $MODE"

if [ "$MODE" = "off" ]; then
  echo "[prop-sim] ARMED=OFF — no new decisions; refreshing the report only"
  $PY -m market_live.remote_report --root "$ROOT"
  exit 0
fi

CONFIG="$ROOT/backend/market_live/config/paper_ftmo_sim_gh.json"
if [ "$MODE" = "real" ]; then
  CONFIG="$ROOT/backend/market_live/config/gh_prop.json"
  : "${PROP_VPS_HOST:?REAL mode needs the PROP_VPS_HOST secret}"
  : "${PROP_VPS_USER:?REAL mode needs the PROP_VPS_USER secret}"
  : "${PROP_VPS_SSH_KEY:?REAL mode needs the PROP_VPS_SSH_KEY secret}"
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  KEY="$HOME/.ssh/prop_vps_key"
  printf '%s\n' "$PROP_VPS_SSH_KEY" > "$KEY" && chmod 600 "$KEY"
  echo "[prop-sim] REAL: tunneling to the MT5 bridge at $PROP_VPS_HOST"
  ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes \
      -f -N -L 8765:127.0.0.1:8765 "$PROP_VPS_USER@$PROP_VPS_HOST"
  BRG=0
  for i in $(seq 1 10); do
    if $PY -c 'import socket; s=socket.socket(); s.settimeout(3); s.connect(("127.0.0.1", 8765)); s.close()' 2>/dev/null; then
      BRG=1; break
    fi
    sleep 2
  done
  if [ "$BRG" != 1 ]; then
    echo "[prop-sim] FATAL: MT5 bridge unreachable through tunnel — NO orders placed"
    exit 3
  fi
  echo "[prop-sim] bridge reachable — trading LIVE on the FTMO account"
fi

$PY -m market_live.run_live live \
    --config "$CONFIG" \
    --feed alpaca --poll-seconds 60 --once

$PY -m market_live.remote_report --root "$ROOT"

echo "[prop-sim] state after step:"
$PY - <<'PY2'
import json, os
for p in ("runtime/state.json", "runtime/state_real.json"):
    if not os.path.exists(p):
        continue
    try:
        d = json.load(open(p))
        b = d.get("book") or {}
        print(f"  {p}: equity={b.get('equity')} cash={b.get('cash')} "
              f"realized={b.get('realized')} fills={b.get('n_fills')}")
        print(f"  {p}: cursor={sorted(set((d.get('last_ts') or {}).values()))}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {p}: unreadable ({exc})")
PY2