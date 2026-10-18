#!/bin/bash
# confirm_best.sh — full-universe OOS confirmation for a chosen cell.
# Usage: ./market_backtest/sweeps/confirm_best.sh <label> <params.json>
#   label:  results subdir + file label (e.g. win_evr_blk)
#   params: JSON file with knob overrides (same format as sweep cells)
set -e
cd "$(dirname "$0")/../.."   # backend/
LABEL="$1"
PARAMS="$2"
if [ -z "$LABEL" ] || [ -z "$PARAMS" ]; then
  echo "usage: confirm_best.sh <label> <params.json>"; exit 1
fi
echo "── stocks ──"
./.venv/bin/python -m market_backtest.batch run --market stocks --label "$LABEL" \
    --params "$PARAMS" --max-workers 8
echo "── perps 1h/1d ──"
./.venv/bin/python -m market_backtest.batch run --market perps --intervals 1d,1h \
    --label "$LABEL" --params "$PARAMS" --max-workers 8
echo "── stats ──"
for M in stocks crypto_perp; do
  IV=$([ "$M" = "stocks" ] && echo 1d || echo 1h)
  ./.venv/bin/python -m market_backtest.stats_report \
      --dir "market_backtest/results/$LABEL" --label "$LABEL" \
      --market "$M" --interval "$IV" --save "stats_${LABEL}_${M}"
done
echo "done → market_backtest/results/$LABEL/"
