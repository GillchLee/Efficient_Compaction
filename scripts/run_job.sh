#!/usr/bin/env bash
# Run (or resume) one Harbor job from a config, with the sweep's settings.
#
#   bash scripts/run_job.sh configs/sweep/luna_ctx80k_rerun_law433.yaml
#
# Same environment as run_luna_ctx_sweep.sh: loads .env, same-conversation
# continuation with binary feedback. Resumes if the job directory already exists.
# Log: jobs/sweep_logs/<job_name>.log; per-turn tables written when it finishes.
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG=${1:?usage: run_job.sh <config.yaml>}
JOB_NAME=$(sed -n 's/^job_name: *//p' "$CONFIG")
JOB_DIR=jobs/$JOB_NAME
LOG=jobs/sweep_logs/$JOB_NAME.log

# shellcheck disable=SC1091
source .venv/bin/activate
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is not set (export it or add it to .env)." >&2
  exit 1
fi
if grep -q '^  type: daytona' "$CONFIG" && [[ -z "${DAYTONA_API_KEY:-}" ]]; then
  echo "DAYTONA_API_KEY is not set (export it or add it to .env)." >&2
  exit 1
fi

export HB_CONTINUE_MODE=same_conversation
export HB_VERIFIER_FEEDBACK_MODE=binary
export DOCKER_DEFAULT_PLATFORM=linux/amd64

mkdir -p jobs/sweep_logs
echo "$(date '+%F %T') start $JOB_NAME ($CONFIG)"
status=0
if [[ -f "$JOB_DIR/config.json" ]]; then
  harbor jobs resume -p "$JOB_DIR" >>"$LOG" 2>&1 || status=$?
else
  harbor run -c "$CONFIG" >>"$LOG" 2>&1 || status=$?
fi
echo "$(date '+%F %T') finished $JOB_NAME (exit $status)"

python scripts/token_turns.py "$JOB_DIR"
exit "$status"
