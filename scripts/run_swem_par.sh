#!/usr/bin/env bash
# SWE-Marathon with parallel re-orientation (docs/parallel_reorientation.md, optlimit/reorient.py):
# Codex + GPT-6 Luna medium, the 6 plan A tasks, 3 h per task, 2 attempts -- the settings of
# luna_swem_cx3_* -- as a 2 x 2: limit policy {fixed 80K, formula + Productive-turn Charge} x
# note behind the compaction summary {none, parallel re-orientation}.
#
#   l80k        fixed 80K, no note                      (control; same settings as luna_swem_cx3_l80k)
#   l80k_par    fixed 80K + --reorient parallel         (effect of the note alone: the limit cannot move)
#   fpt         formula + --productive-turn [+ --reread-premium], no note   (control)
#   fpt_par     the same + --reorient parallel          (the method under test)
#   l80k_plain / fpt_plain   --reorient plain (control wording: "re-orient", nothing about how to fetch)
#
# The controls run again, side by side with the treated arms (same code, same days); the earlier
# luna_swem_cx3_{l80k,formula_pt} runs stay as a second reference.  Each arm is one job with its own
# proxy; its trials run one after the other, so the number of trials running at once = number of arms.
#
#   DRY=1 bash scripts/run_swem_par.sh               # write the configs only
#   setsid nohup bash scripts/run_swem_par.sh &      # write the configs and run the arms side by side
#
# Compliance pilot first (1 h, two tasks with many compactions, treated arms only; compare the
# "phase_window" records with the existing runs):
#   SUFFIX=_pilot HOURS=1 ATTEMPTS=1 TASKS="kubernetes-rust-rewrite rust-java-lsp" ARMS="l80k_par fpt_par" \
#       bash scripts/run_swem_par.sh
# More attempts on the three tasks whose turn counts repeat within ~12% (for total-turn comparisons):
#   SUFFIX=_x TASKS="biofabric-rust-rewrite kubernetes-rust-rewrite rust-java-lsp" bash scripts/run_swem_par.sh
#
# Environment:
#   ARMS       arms to run (default "l80k l80k_par fpt fpt_par")
#   TASKS      tasks (default: the 6 plan A tasks)        ATTEMPTS  attempts per task (default 2)
#   HOURS      agent budget per task (default 3)           SUFFIX    appended to the job names (default none)
#   PREMIUM    1 (default): the formula arms also use --reread-premium (compaction cost += (alpha - 1) x the
#              tokens the re-read calls bring back); 0: Productive-turn Charge alone, as in luna_swem_cx3_formula_pt
#   AGENT_VERSION  Codex CLI release to install (default: the newest at run time).  The earlier controls ran
#              on 0.169.x (luna_swem_cx_l80k, luna_swem_cx3_l80k) and 0.170.0 (luna_swem_cx3_formula_pt):
#              pin the same release when a treated arm is compared with them instead of a fresh control, e.g.
#                ARMS=l80k_par AGENT_VERSION=0.169.0 bash scripts/run_swem_par.sh
#                ARMS=fpt_par  AGENT_VERSION=0.170.0 PREMIUM=0 bash scripts/run_swem_par.sh
#   STAGGER    seconds between arm starts (default 60)
#   OPTLIMIT_PROXY_HOST (default: this host's first address)
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv-h25/bin/python}
ARMS=${ARMS:-"l80k l80k_par fpt fpt_par"}
TASKS=${TASKS:-"kubernetes-rust-rewrite rust-c-compiler biofabric-rust-rewrite nextjs-vite-rewrite ruby-rust-port rust-java-lsp"}
ATTEMPTS=${ATTEMPTS:-2}
HOURS=${HOURS:-3}
SUFFIX=${SUFFIX:-}
PREMIUM=${PREMIUM:-1}
STAGGER=${STAGGER:-60}
export OPTLIMIT_PROXY_HOST=${OPTLIMIT_PROXY_HOST:-$(hostname -I | awk '{print $1}')}
LOG=jobs/logs/luna_cx_chain.log
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

COMMON=(--harness codex --model openai/gpt-6-luna --benchmark swe_marathon --reasoning-effort medium --hours "$HOURS"
        --artifact /workspace/rust-java-lsp --artifact-exclude target)
[ -n "${AGENT_VERSION:-}" ] && COMMON+=(--agent-version "$AGENT_VERSION")
FIXED=(--proxy-fixed 80k)
FORMULA=(--formula codex-luna-medium --prices gpt-6-luna --learn --productive-turn)
[ "$PREMIUM" = 1 ] && FORMULA+=(--reread-premium)

JOBS=()
gen() {  # gen <job> <condition args...>
  local job=$1; shift
  # shellcheck disable=SC2086
  "$PY" configs/make_config.py "${COMMON[@]}" "$@" --attempts "$ATTEMPTS" --tasks $TASKS \
        --job-name "$job" --out "configs/sweep/$job.yaml" >/dev/null || { echo "config failed: $job" >&2; return 1; }
  JOBS+=("$job")
}
for arm in $ARMS; do
  job=luna_swem_cx5_${arm}${SUFFIX}
  case $arm in
    l80k)        gen "$job" "${FIXED[@]}" || exit 1 ;;
    l80k_par)    gen "$job" "${FIXED[@]}" --reorient parallel || exit 1 ;;
    l80k_plain)  gen "$job" "${FIXED[@]}" --reorient plain || exit 1 ;;
    fpt)         gen "$job" "${FORMULA[@]}" || exit 1 ;;
    fpt_par)     gen "$job" "${FORMULA[@]}" --reorient parallel || exit 1 ;;
    fpt_plain)   gen "$job" "${FORMULA[@]}" --reorient plain || exit 1 ;;
    *) echo "unknown arm: $arm" >&2; exit 1 ;;
  esac
done
[ ${#JOBS[@]} -gt 0 ] || { echo "nothing to run" >&2; exit 1; }
echo "configs: ${JOBS[*]/#/configs/sweep/}"
[ "${DRY:-0}" = 1 ] && exit 0

log "LUNA-SWEM-PAR start (${JOBS[*]})"
for j in "${JOBS[@]}"; do
  ( log "start $j"; bash scripts/run_job.sh "configs/sweep/$j.yaml" >>"jobs/logs/$j.chain.log" 2>&1; log "end $j rc=$?" ) &
  sleep "$STAGGER"
done
wait
log "LUNA-SWEM-PAR DONE"
