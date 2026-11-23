#!/usr/bin/env bash
# SWE-Marathon, tasks' own time limit (10 h), terminus-2 + GPT-6 Luna medium, one attempt per job.
# The terminus-2 counterpart of the Codex runs luna_swem_cx4_{l80k,formula_cw}_{javalsp,nextjs}:
# the policy runs inside the agent (optlimit.harbor_agent:OptlimitTerminus2 on stock Harbor 0.25), no proxy.
#
# Two lanes run in parallel; the jobs of one lane run one after another (rust-java-lsp, then nextjs-vite-rewrite):
#   l80k        --limit 80000   (compact when the API-reported prompt reaches 80K)
#   formula_cw  --formula terminus2-gpt-6-luna --prices gpt-6-luna --learn --carry-files 8000 --carry-whole 64000
#               (Re-read Charge on by default; a file re-read after a compaction: its head is carried, at most
#                3K tokens; re-read in two consecutive windows: the whole file, read from disk at the compaction)
#
#   DRY=1 bash scripts/run_swem_t2.sh              # write the configs only
#   setsid nohup bash scripts/run_swem_t2.sh &     # write the configs and run
#
# Environment: LANES (default "l80k formula_cw"); TASKS (default "javalsp nextjs"); STAGGER seconds between
# lane starts (default 60).
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv-h25/bin/python}
LANES=${LANES:-"l80k formula_cw"}
TASKS=${TASKS:-"javalsp nextjs"}
STAGGER=${STAGGER:-60}
LOG=jobs/logs/luna_t2_chain.log
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

BASE=(--model openai/gpt-6-luna --benchmark swe_marathon --reasoning-effort medium --max-tokens 32768 --attempts 1 --hours 10)
JAVA_ART=(--artifact /workspace/rust-java-lsp --artifact-exclude target)
cond() {  # condition arguments of an arm
  case $1 in
    l80k)       echo "--limit 80000" ;;
    formula_cw) echo "--formula terminus2-gpt-6-luna --prices gpt-6-luna --learn --carry-files 8000 --carry-whole 64000" ;;
    *) echo "unknown arm: $1" >&2; return 1 ;;
  esac
}
gen() {  # gen <job> <task> <arm> [extra args...]
  local job=$1 task=$2 arm=$3; shift 3
  local c; c=$(cond "$arm") || return 1
  # shellcheck disable=SC2086
  "$PY" configs/make_config.py "${BASE[@]}" $c "$@" --tasks "$task" \
        --job-name "$job" --out "configs/sweep/$job.yaml" >/dev/null || { echo "config failed: $job" >&2; return 1; }
}
lane_jobs() { local j=(); for t in $TASKS; do j+=("luna_swem_t2_${1}_$t"); done; echo "${j[*]}"; }
for lane in $LANES; do
  for t in $TASKS; do
    case $t in
      javalsp) gen "luna_swem_t2_${lane}_javalsp" rust-java-lsp "$lane" "${JAVA_ART[@]}" || exit 1 ;;
      nextjs)  gen "luna_swem_t2_${lane}_nextjs" nextjs-vite-rewrite "$lane" || exit 1 ;;
      *) echo "unknown task: $t" >&2; exit 1 ;;
    esac
  done
done
for lane in $LANES; do echo "lane $lane: $(lane_jobs "$lane")"; done
[ "${DRY:-0}" = 1 ] && exit 0

mkdir -p jobs/logs
log "LUNA-SWEM-T2 start (lanes: $LANES; tasks: $TASKS)"
for lane in $LANES; do
  (
    for j in $(lane_jobs "$lane"); do
      log "start $j"; bash scripts/run_job.sh "configs/sweep/$j.yaml" >>"jobs/logs/$j.chain.log" 2>&1; log "end $j rc=$?"
    done
    log "lane $lane done"
  ) &
  sleep "$STAGGER"
done
wait
log "LUNA-SWEM-T2 DONE"
