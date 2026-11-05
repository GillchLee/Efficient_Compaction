#!/usr/bin/env bash
# Resume of scripts/_chain_luna10.sh after the 2026-11-04 03:34 disk-quota failure (l140k lost
# after 20 min, l160k/formula never started): rerun 140k, 160k, formula one job at a time; then,
# once the floor/hand-off chain (scripts/_chain_floor_lp.sh) is done, the Lp~10K sweep.
cd "$(dirname "$0")/.."
PARALLEL=1 bash scripts/run_sweep.sh configs/sweep/luna10_l140k.yaml configs/sweep/luna10_l160k.yaml configs/sweep/luna10_formula_nog.yaml
echo "$(date '+%F %T') chain done (140k/160k/formula)"
until grep -q "chain done" jobs/logs/floor_lp_chain.log 2>/dev/null; do sleep 120; done
PARALLEL=1 bash scripts/run_sweep.sh configs/sweep/luna10_lp10k_l60k.yaml configs/sweep/luna10_lp10k_l140k.yaml configs/sweep/luna10_lp10k_l160k.yaml configs/sweep/luna10_lp10k_formula_nog.yaml
echo "$(date '+%F %T') chain done (lp10k)"
