#!/usr/bin/env bash
# SWE-Marathon runs with the tasks' own time limit instead of the 3 h cap used so far.
# Codex + GPT-6 Luna medium, one attempt per job.  The agent is told "You have 10 hours" (5 for zstd-decoder)
# and /app/timer.sh counts down from that, so with these limits the timer and the real cut-off agree.
#
# Four lanes run in parallel; the jobs of one lane run one after another:
#   native      nextjs-vite-rewrite (10 h)  ->  rust-java-lsp (10 h)
#   l80k        nextjs-vite-rewrite (10 h)  ->  rust-java-lsp (10 h)
#   formula     nextjs-vite-rewrite (10 h)  ->  rust-java-lsp (10 h)
#   formula_pt  nextjs-vite-rewrite (10 h)  ->  zstd-decoder native x2 (5 h)
# Settings of each arm are those of plan A (luna_swem_cx3_*) / luna_swem_cx3_formula_pt, except --hours.
#
#   DRY=1 bash scripts/run_swem_long.sh              # write the configs only
#   setsid nohup bash scripts/run_swem_long.sh &     # write the configs and run
#
# Environment: STAGGER seconds between lane starts (default 60); LANES (default "native l80k formula formula_pt").
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv-h25/bin/python}
STAGGER=${STAGGER:-60}
LANES=${LANES:-"native l80k formula formula_pt"}
export OPTLIMIT_PROXY_HOST=${OPTLIMIT_PROXY_HOST:-143.248.56.19}
LOG=jobs/logs/luna_cx_chain.log
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

BASE=(--harness codex --model openai/gpt-6-luna --benchmark swe_marathon --reasoning-effort medium --attempts 1)
JAVA_ART=(--artifact /workspace/rust-java-lsp --artifact-exclude target)
FORMULA=(--formula codex-luna-medium --prices gpt-6-luna --learn)
cond() {  # condition arguments of an arm
  case $1 in
    native)     echo "--limit native --observe" ;;
    l80k)       echo "--proxy-fixed 80k" ;;
    formula)    echo "${FORMULA[*]}" ;;
    formula_pt) echo "${FORMULA[*]} --productive-turn" ;;
    *) echo "unknown arm: $1" >&2; return 1 ;;
  esac
}
gen() {  # gen <job> <hours> <task> <attempts> <arm> [extra args...]
  local job=$1 hours=$2 task=$3 att=$4 arm=$5; shift 5
  local c; c=$(cond "$arm") || return 1
  # shellcheck disable=SC2086
  "$PY" configs/make_config.py "${BASE[@]}" --hours "$hours" $c "$@" --attempts "$att" --tasks "$task" \
        --job-name "$job" --out "configs/sweep/$job.yaml" >/dev/null || { echo "config failed: $job" >&2; return 1; }
}
lane_jobs() {  # jobs of a lane, in order
  case $1 in
    native|l80k|formula) echo "luna_swem_cx4_${1}_nextjs luna_swem_cx4_${1}_javalsp" ;;
    formula_pt)          echo "luna_swem_cx4_formula_pt_nextjs luna_swem_cx4_native_zstd" ;;
  esac
}
for lane in $LANES; do
  gen "luna_swem_cx4_${lane}_nextjs" 10 nextjs-vite-rewrite 1 "$lane" || exit 1
  case $lane in
    native|l80k|formula) gen "luna_swem_cx4_${lane}_javalsp" 10 rust-java-lsp 1 "$lane" "${JAVA_ART[@]}" || exit 1 ;;
    formula_pt)          gen luna_swem_cx4_native_zstd 5 zstd-decoder 2 native || exit 1 ;;
  esac
done
for lane in $LANES; do echo "lane $lane: $(lane_jobs "$lane")"; done
[ "${DRY:-0}" = 1 ] && exit 0

log "LUNA-SWEM-LONG start (lanes: $LANES)"
for lane in $LANES; do
  ( for j in $(lane_jobs "$lane"); do
      log "start $j"; bash scripts/run_job.sh "configs/sweep/$j.yaml" >>"jobs/logs/$j.chain.log" 2>&1; log "end $j rc=$?"
    done ) &
  sleep "$STAGGER"
done
wait
log "LUNA-SWEM-LONG DONE"
