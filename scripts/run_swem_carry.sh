#!/usr/bin/env bash
# SWE-Marathon, tasks' own time limit (10 h), Codex + GPT-6 Luna medium, one attempt per job:
# the formula arm of scripts/run_swem_long.sh (luna_swem_cx4_formula_*) plus File Carry-over with
# Whole-file Carry-over.  Everything else is identical, so the jobs compare directly with
# luna_swem_cx4_{native,l80k,formula}_{javalsp,nextjs}.
#
#   formula_cw   --carry-files 8000 --carry-whole 64000
#                (a file re-read after a compaction: its last read output is carried, at most 3K tokens;
#                 re-read in two consecutive windows: the whole file, read from disk at the compaction)
#
# The jobs run one after another (rust-java-lsp, then nextjs-vite-rewrite).
#
#   DRY=1 bash scripts/run_swem_carry.sh              # write the configs only
#   setsid nohup bash scripts/run_swem_carry.sh &     # write the configs and run
#
# Environment: TASKS (default "javalsp nextjs"); OPTLIMIT_PROXY_HOST (default: this host's first address).
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv-h25/bin/python}
TASKS=${TASKS:-"javalsp nextjs"}
export OPTLIMIT_PROXY_HOST=${OPTLIMIT_PROXY_HOST:-$(hostname -I | awk '{print $1}')}
LOG=jobs/logs/luna_cx_chain.log
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

BASE=(--harness codex --model openai/gpt-6-luna --benchmark swe_marathon --reasoning-effort medium --attempts 1)
JAVA_ART=(--artifact /workspace/rust-java-lsp --artifact-exclude target)
ARM=(--formula codex-luna-medium --prices gpt-6-luna --learn --carry-files 8000 --carry-whole 64000)
gen() {  # gen <job> <task> [extra args...]
  local job=$1 task=$2; shift 2
  "$PY" configs/make_config.py "${BASE[@]}" --hours 10 "${ARM[@]}" "$@" --tasks "$task" \
        --job-name "$job" --out "configs/sweep/$job.yaml" >/dev/null || { echo "config failed: $job" >&2; return 1; }
}
JOBS=()
for t in $TASKS; do
  case $t in
    javalsp) gen luna_swem_cx4_formula_cw_javalsp rust-java-lsp "${JAVA_ART[@]}" || exit 1 ;;
    nextjs)  gen luna_swem_cx4_formula_cw_nextjs nextjs-vite-rewrite || exit 1 ;;
    *) echo "unknown task: $t" >&2; exit 1 ;;
  esac
  JOBS+=("luna_swem_cx4_formula_cw_$t")
done
echo "jobs: ${JOBS[*]}"
[ "${DRY:-0}" = 1 ] && exit 0

log "LUNA-SWEM-CARRY start (${JOBS[*]})"
for j in "${JOBS[@]}"; do
  log "start $j"; bash scripts/run_job.sh "configs/sweep/$j.yaml" >>"jobs/logs/$j.chain.log" 2>&1; log "end $j rc=$?"
done
log "LUNA-SWEM-CARRY DONE"
