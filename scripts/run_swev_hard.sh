#!/usr/bin/env bash
# SWE-bench Verified "hard" subset (45 tasks labelled 1-4 h / >4 h), Codex + GPT-6 Luna medium,
# several compaction arms side by side, each task ATTEMPTS times (mean over attempts).
# For a host without Docker (Singularity/Apptainer, docs/singularity.md); ENV=docker also works.
#
#   .venv-h25/bin/python scripts/prep_swebench.py --singularity --subset hard --pull   # once
#   CONC=6 setsid nohup bash scripts/run_swev_hard.sh >/dev/null 2>&1 &
#
#   CONC      concurrent trials per arm (default 4; RAM: arms x CONC x ~8 GB)
#   ATTEMPTS  runs per task (default 2)
#   ARMS      default "native l80k formula balnet"; also l40k, balance, formula_carry
#   PREFIX    job-name prefix (default swev_hard_cx)
#   EFFORT    Codex reasoning effort (default medium)
# Progress: jobs/logs/$PREFIX.chain.log; per arm jobs/<job>/ and jobs/logs/<job>.*
set -uo pipefail
cd "$(dirname "$0")/.."
CONC=${CONC:-4}; ATTEMPTS=${ATTEMPTS:-2}; ARMS=${ARMS:-native l80k formula balnet}
PREFIX=${PREFIX:-swev_hard_cx}; EFFORT=${EFFORT:-medium}; ENV=${ENV:-singularity}
BENCH=swebench_verified_sif; [[ $ENV == docker ]] && BENCH=swebench_verified
TASKS_DIR=data/swebench-verified-sif; [[ $ENV == docker ]] && TASKS_DIR=data/swebench-verified
PY=.venv-h25/bin/python
LOG=jobs/logs/$PREFIX.chain.log
mkdir -p jobs/logs configs/sweep
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

mapfile -t TASKS < <(ls "$TASKS_DIR" 2>/dev/null)
[[ ${#TASKS[@]} -gt 0 ]] || { echo "no tasks in $TASKS_DIR: run scripts/prep_swebench.py first" >&2; exit 1; }
COMMON=(--harness codex --model openai/gpt-6-luna --benchmark "$BENCH" --env "$ENV" --reasoning-effort "$EFFORT"
        --concurrent "$CONC" --attempts "$ATTEMPTS" --tasks "${TASKS[@]}")
LEARN=(--prices gpt-6-luna --learn)
PRIOR=codex-luna-$EFFORT
log "start $PREFIX: ${#TASKS[@]} tasks x $ATTEMPTS attempts, arms [$ARMS], $CONC concurrent per arm, env $ENV"
for arm in $ARMS; do
  case $arm in
    native)        args=(--limit native --observe) ;;
    l80k)          args=(--proxy-fixed 80k) ;;
    l40k)          args=(--proxy-fixed 40k) ;;
    formula)       args=(--formula "$PRIOR" "${LEARN[@]}") ;;
    formula_carry) args=(--formula "$PRIOR" "${LEARN[@]}" --carry-files) ;;
    balance)       args=(--balance "$PRIOR" "${LEARN[@]}") ;;
    balnet)        args=(--balance "$PRIOR" "${LEARN[@]}" --net-rent) ;;
    *) log "unknown arm $arm"; continue ;;
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
