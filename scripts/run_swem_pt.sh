#!/usr/bin/env bash
# SWE-Marathon follow-up to plan A (luna_swem_cx3_*): Codex + GPT-6 Luna medium, same 6 tasks, 3 h per task.
#
#   formula_pt   formula + learn + Productive-turn Charge (--productive-turn): the limit that minimises the
#                cost per turn that is not a re-read.  Compare with luna_swem_cx3_formula (same settings
#                otherwise) and with luna_swem_cx3_native / luna_swem_cx3_l80k.
#   l130k        fixed 130K limit (5-8 compactions per task instead of 20-35): shows whether the extra turns
#                grow with the number of compactions, i.e. how many turns one compaction wastes.
#   rerun_*      one more attempt of plan A trials that did not produce a usable score (e.g. the grader's
#                cached-golden scan zeroing rust-java-lsp).  Results go to luna_swem_cx3r_<arm>, they do not
#                replace the plan A trials.
#
# Every job copies /workspace/rust-java-lsp (without target/) out before the verifier runs, so a trial the
# scan zeroes can still be graded again.  Other tasks do not have that path; Harbor records it as "failed"
# in artifacts/manifest.json and carries on.
#
#   DRY=1 bash scripts/run_swem_pt.sh                  # write the configs only
#   setsid nohup bash scripts/run_swem_pt.sh &         # write the configs and run the arms in parallel
#
# Environment:
#   ARMS            arms to run (default "formula_pt l130k rerun_formula")
#                   formula_pt | l130k | balnet_pt | rerun_native | rerun_l80k | rerun_formula
#   ATTEMPTS        attempts per task of the *_pt arms (default 2, as in plan A)
#   TASKS           tasks of the *_pt arms (default: the 6 plan A tasks)
#   RERUN_NATIVE / RERUN_L80K / RERUN_FORMULA   tasks to run once more (default: formula rust-java-lsp)
#   STAGGER         seconds between arm starts (default 60)
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv-h25/bin/python}
ARMS=${ARMS:-"formula_pt l130k rerun_formula"}
ATTEMPTS=${ATTEMPTS:-2}
TASKS=${TASKS:-"kubernetes-rust-rewrite rust-c-compiler biofabric-rust-rewrite nextjs-vite-rewrite ruby-rust-port rust-java-lsp"}
RERUN_NATIVE=${RERUN_NATIVE:-""}
RERUN_L80K=${RERUN_L80K:-""}
RERUN_FORMULA=${RERUN_FORMULA:-"rust-java-lsp"}
STAGGER=${STAGGER:-60}
export OPTLIMIT_PROXY_HOST=${OPTLIMIT_PROXY_HOST:-143.248.56.19}
LOG=jobs/logs/luna_cx_chain.log
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

COMMON=(--harness codex --model openai/gpt-6-luna --benchmark swe_marathon --reasoning-effort medium --hours 3
        --artifact /workspace/rust-java-lsp --artifact-exclude target)
FORMULA=(--formula codex-luna-medium --prices gpt-6-luna --learn)

JOBS=()
gen() {  # gen <job> <attempts> "<tasks>" <condition args...>
  local job=$1 att=$2 tasks=$3; shift 3
  # shellcheck disable=SC2086
  "$PY" configs/make_config.py "${COMMON[@]}" "$@" --attempts "$att" --tasks $tasks \
        --job-name "$job" --out "configs/sweep/$job.yaml" >/dev/null || { echo "config failed: $job" >&2; return 1; }
  JOBS+=("$job")
}

for arm in $ARMS; do
  case $arm in
    formula_pt)    gen luna_swem_cx3_formula_pt "$ATTEMPTS" "$TASKS" "${FORMULA[@]}" --productive-turn || exit 1 ;;
    l130k)         gen luna_swem_cx3_l130k "$ATTEMPTS" "$TASKS" --proxy-fixed 130k || exit 1 ;;
    balnet_pt)     gen luna_swem_cx3_balnet_pt "$ATTEMPTS" "$TASKS" --balance codex-luna-medium --prices gpt-6-luna --learn --net-rent --productive-turn || exit 1 ;;
    rerun_native)  if [ -n "$RERUN_NATIVE" ]; then gen luna_swem_cx3r_native 1 "$RERUN_NATIVE" --limit native --observe || exit 1; fi ;;
    rerun_l80k)    if [ -n "$RERUN_L80K" ]; then gen luna_swem_cx3r_l80k 1 "$RERUN_L80K" --proxy-fixed 80k || exit 1; fi ;;
    rerun_formula) if [ -n "$RERUN_FORMULA" ]; then gen luna_swem_cx3r_formula 1 "$RERUN_FORMULA" "${FORMULA[@]}" || exit 1; fi ;;
    *) echo "unknown arm: $arm" >&2; exit 1 ;;
  esac
done
[ ${#JOBS[@]} -gt 0 ] || { echo "nothing to run" >&2; exit 1; }
echo "configs: ${JOBS[*]/#/configs/sweep/}"
[ "${DRY:-0}" = 1 ] && exit 0

log "LUNA-SWEM-PT start (${JOBS[*]})"
for j in "${JOBS[@]}"; do
  ( log "start $j"; bash scripts/run_job.sh "configs/sweep/$j.yaml" >>"jobs/logs/$j.chain.log" 2>&1; log "end $j rc=$?" ) &
  sleep "$STAGGER"
done
wait
log "LUNA-SWEM-PT DONE"
