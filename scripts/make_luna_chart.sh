#!/usr/bin/env bash
# Combined per-turn chart page for every GPT-6 Luna LHTB sweep (batches A, B, B', M, C).
# Usage: bash scripts/make_luna_chart.sh [out.html]   (default jobs/luna_sweeps.html)
# Needs token_turns/ in each job (analysis/token_turns.py) and docs/luna_sweeps_intro.html (summary tables).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:-jobs/luna_sweeps.html}
.venv/bin/python analysis/chart.py \
  jobs/luna10c_l15k jobs/luna10c_l25k jobs/luna10c_l40k jobs/luna10c_f_stock jobs/luna10c_f_single jobs/luna10c_f_single2k \
  jobs/luna10_l60k jobs/luna10_l140k jobs/luna10_l160k jobs/luna10_formula_nog \
  jobs/luna10_lp10k_l60k jobs/luna10_lp10k_l140k jobs/luna10_lp10k_l160k jobs/luna10_lp10k_formula_nog \
  jobs/lhtb-luna-ctx80k jobs/lhtb-luna-ctx80k-rerun-law433 jobs/lhtb-luna-ctx120k jobs/lhtb-luna-ctxnative \
  jobs/luna20_formula jobs/luna20_l80k jobs/luna20_native \
  --cond luna10c_l15k="A l15k" --cond luna10c_l25k="A l25k" --cond luna10c_l40k="A l40k" \
  --cond luna10c_f_stock="A formula" --cond luna10c_f_single="A single" --cond luna10c_f_single2k="A single2k" \
  --cond luna10_l60k="B l60k" --cond luna10_l140k="B l140k" --cond luna10_l160k="B l160k" --cond luna10_formula_nog="B formula" \
  --cond luna10_lp10k_l60k="B′ lp10k l60k" --cond luna10_lp10k_l140k="B′ lp10k l140k" \
  --cond luna10_lp10k_l160k="B′ lp10k l160k" --cond luna10_lp10k_formula_nog="B′ lp10k formula" \
  --cond lhtb-luna-ctx80k="M l80k" --cond lhtb-luna-ctx80k-rerun-law433="M l80k 재실행" \
  --cond lhtb-luna-ctx120k="M l120k" --cond lhtb-luna-ctxnative="M native" \
  --cond luna20_formula="C formula" --cond luna20_l80k="C l80k" --cond luna20_native="C native" \
  --title "Luna 압축 창 sweep" --intro docs/luna_sweeps_intro.html --out "$OUT"
