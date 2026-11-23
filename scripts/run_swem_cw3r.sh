#!/usr/bin/env bash
# One more attempt of luna_swem_cx3_formula_cw_{a,b} trials that failed while Codex was being installed
# (no model call was made).  Same settings as scripts/run_swem_cw3.sh; the results go to
# luna_swem_cx3r_formula_cw_<n> and do not replace anything.
#
#   DRY=1 bash scripts/run_swem_cw3r.sh              # write the configs only
#   setsid nohup bash scripts/run_swem_cw3r.sh &     # write the configs and run, one job per task side by side
#
# Environment: RERUN (tasks, default "biofabric-rust-rewrite nextjs-vite-rewrite"), STAGGER (default 60),
#              OPTLIMIT_PROXY_HOST (default: this host's first address)
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv-h25/bin/python}
RERUN=${RERUN:-"biofabric-rust-rewrite nextjs-vite-rewrite"}
STAGGER=${STAGGER:-60}
export OPTLIMIT_PROXY_HOST=${OPTLIMIT_PROXY_HOST:-$(hostname -I | awk '{print $1}')}
LOG=jobs/logs/luna_cx_chain.log
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

COMMON=(--harness codex --model openai/gpt-6-luna --benchmark swe_marathon --reasoning-effort medium --hours 3
        --artifact /workspace/rust-java-lsp --artifact-exclude target)
ARM=(--formula codex-luna-medium --prices gpt-6-luna --learn --carry-files 8000 --carry-whole 64000)

JOBS=(); n=0
for t in $RERUN; do
  n=$((n+1)); job=luna_swem_cx3r_formula_cw_$n
  "$PY" configs/make_config.py "${COMMON[@]}" "${ARM[@]}" --attempts 1 --tasks "$t" \
        --job-name "$job" --out "configs/sweep/$job.yaml" >/dev/null || { echo "config failed: $job" >&2; exit 1; }
  JOBS+=("$job")
done
echo "configs: ${JOBS[*]/#/configs/sweep/}"
[ "${DRY:-0}" = 1 ] && exit 0

log "LUNA-SWEM-CW3R start (${JOBS[*]})"
for j in "${JOBS[@]}"; do
  ( log "start $j"; bash scripts/run_job.sh "configs/sweep/$j.yaml" >>"jobs/logs/$j.chain.log" 2>&1; log "end $j rc=$?" ) &
  sleep "$STAGGER"
done
wait
log "LUNA-SWEM-CW3R DONE"
