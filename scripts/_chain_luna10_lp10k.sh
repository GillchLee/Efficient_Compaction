#!/usr/bin/env bash
# One-off: after the first LHTB 1.0 chain (scripts/_chain_luna10.sh) finishes, run the same four
# conditions with the bigger hand-off (Lp ~10K: summary 2600 words, >=12 questions, answers 2000 words).
cd "$(dirname "$0")/.."
until grep -q "chain done" jobs/logs/luna10_chain.log 2>/dev/null; do sleep 120; done
PARALLEL=1 bash scripts/run_sweep.sh configs/sweep/luna10_lp10k_l60k.yaml configs/sweep/luna10_lp10k_l140k.yaml configs/sweep/luna10_lp10k_l160k.yaml configs/sweep/luna10_lp10k_formula_nog.yaml
echo "$(date '+%F %T') chain done"
