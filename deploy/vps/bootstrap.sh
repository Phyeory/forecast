#!/bin/bash
# Bootstrap the paper-trading VPS (Ubuntu 24.04, GCP e2-micro / Oracle ARM).
# Idempotent — safe to re-run.
set -euo pipefail

REPO_URL="https://github.com/Phyeory/forecast.git"
BRANCH="market-backtest-systems"
HOME_DIR="$HOME"
REPO_DIR="$HOME_DIR/pump-chart-market"
BACKEND="$REPO_DIR/backend"
KEYS="$BACKEND/market_live/config/keys.env"

echo "=== [1/7] system packages ==="
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git cron ca-certificates >/dev/null
python3 --version

echo "=== [2/7] clone repo ($BRANCH) ==="
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
else
  cd "$REPO_DIR" && git fetch --depth 1 origin "$BRANCH" && git checkout "$BRANCH" && git reset --hard origin/"$BRANCH"
fi
cd "$REPO_DIR" && git log --oneline -1

echo "=== [3/7] python env (numpy + scipy: scipy is required for engine parity) ==="
cd "$BACKEND"
if [ ! -d "$BACKEND/.venv" ]; then
  python3 -m venv "$BACKEND/.venv"
fi
"$BACKEND/.venv/bin/pip" install -q --upgrade pip
"$BACKEND/.venv/bin/pip" install -q "numpy>=2.0" "scipy>=1.13"

echo "=== [4/7] keys ==="
if [ ! -f "$KEYS" ]; then
  echo "!! $KEYS missing — copy it in before the first run (scp from the Mac)"
  exit 1
fi
chmod 600 "$KEYS"
mkdir -p "$BACKEND/data/live_market"

echo "=== [5/7] timezone + cron (16:40 ET, Mon-Fri — after the US close) ==="
sudo timedatectl set-timezone America/New_York || true
cat > "$HOME_DIR/market-paper-cron" <<'CRON'
CRON_TZ=America/New_York
# 16:40 ET Mon-Fri: process the just-closed US bar, submit next-open orders.
40 16 * * 1-5 /home/ubuntu/pump-chart-market/backend/market_live/daily_alpaca.sh >> /home/ubuntu/pump-chart-market/backend/data/live_market/cron.log 2>&1
# 20:10 ET safety pass: catches a bar that was not finalised at 16:40.
10 20 * * 1-5 /home/ubuntu/pump-chart-market/backend/market_live/daily_alpaca.sh >> /home/ubuntu/pump-chart-market/backend/data/live_market/cron.log 2>&1
CRON
# fix the hard-coded home path for whatever user this is
sed -i "s#/home/ubuntu#$HOME_DIR#g" "$HOME_DIR/market-paper-cron"
crontab "$HOME_DIR/market-paper-cron" 2>/dev/null || { crontab -l 2>/dev/null; cat "$HOME_DIR/market-paper-cron"; } > /dev/null
crontab -l
sudo service cron restart || sudo systemctl restart cron || true

echo "=== [6/7] runner script ==="
cat > "$HOME_DIR/run-market-paper.sh" <<'RUN'
#!/bin/bash
set -euo pipefail
cd "$HOME/pump-chart-market/backend"
source market_live/config/keys.env
PY="$HOME/pump-chart-market/backend/.venv/bin/python"
echo "=== $(date -u) market paper run ==="
$PY -m market_live.run_live live --config market_live/config/alpaca_paper.json --feed alpaca --once
$PY -m market_live.challenge_status --latest || true
RUN
chmod +x "$HOME_DIR/run-market-paper.sh"
# make the shipped daily script use the venv python
sed -i "s#^PY=.*#PY=$BACKEND/.venv/bin/python#" "$BACKEND/market_live/daily_alpaca.sh" 2>/dev/null || true

echo "=== [7/7] self-test (warm 1 symbol, no orders) ==="
cd "$BACKEND"
set +e
"$BACKEND/.venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, ".")
from market_live.brokers import AlpacaBroker
b = AlpacaBroker(paper=True)
st = b.state()
print("broker state:", st)
print("symbols resolvable:", len(b.symbols()))
PY
set -e
echo
echo "=== BOOTSTRAP COMPLETE ==="
echo "next: scp keys.env, then run: $HOME_DIR/run-market-paper.sh"
