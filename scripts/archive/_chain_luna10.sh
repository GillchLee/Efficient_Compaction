#!/usr/bin/env bash
# One-off: after jobs/luna_formula_nog finishes, run the LHTB 1.0 sweep (60k, 140k, 160k, formula) one job at a time.
cd "$(dirname "$0")/.."
until grep -q "finished luna_formula_nog" jobs/logs/luna_formula_nog_launcher.log 2>/dev/null; do sleep 60; done
PARALLEL=1 bash scripts/run_sweep.sh configs/sweep/luna10_l60k.yaml configs/sweep/luna10_l140k.yaml configs/sweep/luna10_l160k.yaml configs/sweep/luna10_formula_nog.yaml
echo "$(date '+%F %T') chain done"
