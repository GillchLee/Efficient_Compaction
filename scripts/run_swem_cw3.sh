#!/usr/bin/env bash
# SWE-Marathon plan A tasks with formula + carry-over: Codex + GPT-6 Luna medium, the 6 plan A tasks,
# 3 h per task, 2 attempts -- the settings of luna_swem_cx3_formula plus File Carry-over with Whole-file
# Carry-over (--carry-files 8000 --carry-whole 64000).  Compare with luna_swem_cx3_{native,l80k,formula},
# luna_swem_cx3_formula_pt and luna_swem_cx3_l130k.
#
# The tasks are split over lanes that run side by side (each lane is one job with its own proxy); the
# trials themselves are configured exactly as in plan A.
#
#   DRY=1 bash scripts/run_swem_cw3.sh               # write the configs only
#   setsid nohup bash scripts/run_swem_cw3.sh &      # write the configs and run
#
# Environment:
#   LANE_A / LANE_B   tasks of each lane (an empty lane is skipped)
#   ATTEMPTS          attempts per task (default 2, as in plan A)
#   STAGGER           seconds between lane starts (default 60)
#   OPTLIMIT_PROXY_HOST (default: this host's first address)
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv-h25/bin/python}
LANE_A=${LANE_A-"kubernetes-rust-rewrite rust-c-compiler biofabric-rust-rewrite"}
LANE_B=${LANE_B-"nextjs-vite-rewrite ruby-rust-port rust-java-lsp"}
ATTEMPTS=${ATTEMPTS:-2}
STAGGER=${STAGGER:-60}
export OPTLIMIT_PROXY_HOST=${OPTLIMIT_PROXY_HOST:-$(hostname -I | awk '{print $1}')}
LOG=jobs/logs/luna_cx_chain.log
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

COMMON=(--harness codex --model openai/gpt-6-luna --benchmark swe_marathon --reasoning-effort medium --hours 3
        --artifact /workspace/rust-java-lsp --artifact-exclude target)
ARM=(--formula codex-luna-medium --prices gpt-6-luna --learn --carry-files 8000 --carry-whole 64000)

JOBS=()
gen() {  # gen <job> "<tasks>"
  local job=$1 tasks=$2
  # shellcheck disable=SC2086
  "$PY" configs/make_config.py "${COMMON[@]}" "${ARM[@]}" --attempts "$ATTEMPTS" --tasks $tasks \
        --job-name "$job" --out "configs/sweep/$job.yaml" >/dev/null || { echo "config failed: $job" >&2; return 1; }
  JOBS+=("$job")
}
[ -n "$LANE_A" ] && { gen luna_swem_cx3_formula_cw_a "$LANE_A" || exit 1; }
[ -n "$LANE_B" ] && { gen luna_swem_cx3_formula_cw_b "$LANE_B" || exit 1; }
[ ${#JOBS[@]} -gt 0 ] || { echo "nothing to run" >&2; exit 1; }
echo "configs: ${JOBS[*]/#/configs/sweep/}"
[ "${DRY:-0}" = 1 ] && exit 0

log "LUNA-SWEM-CW3 start (${JOBS[*]})"
for j in "${JOBS[@]}"; do
  ( log "start $j"; bash scripts/run_job.sh "configs/sweep/$j.yaml" >>"jobs/logs/$j.chain.log" 2>&1; log "end $j rc=$?" ) &
  sleep "$STAGGER"
done
wait
log "LUNA-SWEM-CW3 DONE"
