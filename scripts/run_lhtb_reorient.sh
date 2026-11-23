#!/usr/bin/env bash
# LHTB 2.0 with Codex behind the optlimit proxy: the same compaction policy with and without the
# re-orientation instruction (docs/parallel_reorientation.md), arms side by side.
# For a host without Docker (Singularity/Apptainer, docs/singularity.md); ENV=docker also works.
#
#   TASKS="seismo_cross_correlation_lag__01" HOURS=0.5 ARMS="l40k_par" bash scripts/run_lhtb_reorient.sh   # smoke
#   CONC=4 setsid nohup bash scripts/run_lhtb_reorient.sh >/dev/null 2>&1 &
#
#   ARMS      "<policy>_<reorient>" list; policy: l40k | l80k | formula | balance, reorient: base | seq | par
#             (default "l40k_base l40k_seq l40k_par"). A fixed window keeps the number of compactions equal
#             across arms; formula/balance let the controller move the window.
#   TASKS     task names (default: every task in LHTB/tasks-2.0 the proxy can reach)
#   CONC      concurrent trials per arm (default 2; RAM: arms x CONC x ~8 GB)
#   ATTEMPTS  runs per task (default 1)      HOURS  agent budget per task (default 3)
#   MODEL     default openai/gpt-6-luna      EFFORT Codex reasoning effort (default medium)
#   PREFIX    job-name prefix (default lhtb2_cx)
# Progress: jobs/logs/$PREFIX.chain.log; per arm jobs/<job>/ and jobs/logs/<job>.*
# Per-turn width after each compaction: analysis/reorient_turns.py jobs/logs/<job>.proxy.jsonl
set -uo pipefail
cd "$(dirname "$0")/.."
CONC=${CONC:-2}; ATTEMPTS=${ATTEMPTS:-1}; HOURS=${HOURS:-3}; ARMS=${ARMS:-l40k_base l40k_seq l40k_par}
PREFIX=${PREFIX:-lhtb2_cx}; EFFORT=${EFFORT:-medium}; ENV=${ENV:-singularity}; MODEL=${MODEL:-openai/gpt-6-luna}
PRICES=${PRICES:-gpt-6-luna}; PRIOR=${PRIOR:-codex-luna-$EFFORT}
PY=.venv-h25/bin/python; HARBOR=(--harbor 0.25)
[[ $ENV == docker && -z ${H25:-} ]] && { PY=.venv/bin/python; HARBOR=(); }
LOG=jobs/logs/$PREFIX.chain.log
mkdir -p jobs/logs configs/sweep
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

COMMON=(--harness codex --model "$MODEL" --benchmark lhtb2 "${HARBOR[@]}" --env "$ENV" --reasoning-effort "$EFFORT"
        --concurrent "$CONC" --attempts "$ATTEMPTS" --hours "$HOURS")
[[ -n ${TASKS:-} ]] && COMMON+=(--tasks $TASKS)
LEARN=(--prices "$PRICES" --learn)
log "start $PREFIX: arms [$ARMS], tasks [${TASKS:-all}] x $ATTEMPTS, $HOURS h, $CONC concurrent per arm, env $ENV"
for arm in $ARMS; do
  pol=${arm%_*}; re=${arm##*_}
  case $pol in
    l40k)    args=(--proxy-fixed 40k) ;;
    l80k)    args=(--proxy-fixed 80k) ;;
    formula) args=(--formula "$PRIOR" "${LEARN[@]}") ;;
    balance) args=(--balance "$PRIOR" "${LEARN[@]}") ;;
    *) log "unknown policy in arm $arm"; continue ;;
  esac
  case $re in
    base) ;;
    seq)  args+=(--reorient sequential) ;;
    par)  args+=(--reorient parallel) ;;
    *) log "unknown re-orientation in arm $arm (base | seq | par)"; continue ;;
  esac
  job=${PREFIX}_$arm
  $PY configs/make_config.py "${COMMON[@]}" "${args[@]}" --job-name "$job" --out "configs/sweep/$job.yaml" >/dev/null \
    || { log "config failed: $job"; continue; }
  ( log "start $job"; bash scripts/run_job.sh "configs/sweep/$job.yaml" >>"jobs/logs/$job.chain.log" 2>&1
    log "end $job rc=$?" ) &
  sleep 60     # stagger proxy start-up and the first image reads
done
wait
log "$PREFIX done"
