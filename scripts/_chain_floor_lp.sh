#!/usr/bin/env bash
# Window floor (15/25/40K, stock hand-off) and hand-off design (stock 3-call / single / single 2k words,
# formula with learned terms) on three LHTB 1.0 tasks with reward curves, 2 h each, two jobs at a time.
cd "$(dirname "$0")/.."
PARALLEL=2 bash scripts/run_sweep.sh configs/sweep/luna10c_l15k.yaml configs/sweep/luna10c_f_single.yaml \
  configs/sweep/luna10c_l25k.yaml configs/sweep/luna10c_f_stock.yaml \
  configs/sweep/luna10c_l40k.yaml configs/sweep/luna10c_f_single2k.yaml
echo "$(date '+%F %T') chain done"
