#!/usr/bin/env bash
# Run (or resume) one Harbor job from a config.
#
#   bash scripts/run_job.sh configs/sweep/<job>.yaml
#   setsid nohup bash scripts/run_job.sh configs/sweep/<job>.yaml > /dev/null 2>&1 &   # detached (Linux)
#   nohup caffeinate -ims bash scripts/run_job.sh ... &                                 # macOS
#
# Loads .env, checks the API key the model needs and that the environment (docker /
# daytona) is usable, sets same-conversation continuation with binary feedback, resumes
# if the job directory already exists, then writes the per-turn tables.
# Log: jobs/logs/<job_name>.log
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG=${1:?usage: run_job.sh <config.yaml>}
JOB_NAME=$(sed -n 's/^job_name: *//p' "$CONFIG")
JOB_DIR=jobs/$JOB_NAME
LOG=jobs/logs/$JOB_NAME.log

# shellcheck disable=SC1091
source .venv/bin/activate
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

MODEL=$(sed -n 's/^ *model_name: *//p' "$CONFIG" | head -1)
case "$MODEL" in
  anthropic/*) [[ -n "${ANTHROPIC_API_KEY:-}" ]] || { echo "ANTHROPIC_API_KEY is not set (.env)" >&2; exit 1; } ;;
  openai/*)    [[ -n "${OPENAI_API_KEY:-}" ]]    || { echo "OPENAI_API_KEY is not set (.env)" >&2; exit 1; } ;;
esac
if grep -q '^  type: daytona' "$CONFIG"; then
  [[ -n "${DAYTONA_API_KEY:-}" ]] || { echo "DAYTONA_API_KEY is not set (.env)" >&2; exit 1; }
elif ! docker info >/dev/null 2>&1; then
  echo "Docker is not running." >&2
  exit 1
fi

export HB_CONTINUE_MODE=${HB_CONTINUE_MODE-same_conversation}
export HB_VERIFIER_FEEDBACK_MODE=binary
export DOCKER_DEFAULT_PLATFORM=linux/amd64

mkdir -p jobs/logs
echo "$(date '+%F %T') start $JOB_NAME ($CONFIG, HB_CONTINUE_MODE=$HB_CONTINUE_MODE)" | tee -a "$LOG"
status=0
if [[ -f "$JOB_DIR/config.json" ]]; then
  harbor jobs resume -p "$JOB_DIR" >>"$LOG" 2>&1 || status=$?
else
  harbor run -c "$CONFIG" >>"$LOG" 2>&1 || status=$?
fi
echo "$(date '+%F %T') finished $JOB_NAME (exit $status)" | tee -a "$LOG"

python analysis/token_turns.py "$JOB_DIR"
exit "$status"
