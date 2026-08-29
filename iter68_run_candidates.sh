#!/bin/zsh
# Launch the primary candidate (hf_silence=2700) as soon as iter68_base completes.
cd /Users/jaime/pump-chart
while pgrep -f "run_iteration.py --label iter68_base" > /dev/null; do sleep 30; done
echo "baseline done at $(date)"
cat > /tmp/iter68_hfs2700.json << 'JSON'
{"v2_hf_silence_gate_seconds": 2700.0}
JSON
BACKTEST_RESULTS_DIR=backend/v2_results backend/.venv/bin/python run_iteration.py \
  --label iter68_hfs2700 --params /tmp/iter68_hfs2700.json \
  --recording-ids-file backend/analysis/iter48_cohort_full.json --max-workers 8 \
  > backend/analysis/iter68_hfs2700_run.log 2>&1
echo "candidate done at $(date)"
