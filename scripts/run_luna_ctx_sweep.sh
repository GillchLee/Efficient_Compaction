#!/usr/bin/env bash
# Compaction-window sweep (40k / 80k / 120k / native) for GPT-6 luna on terminus-2.
#
#   bash scripts/run_luna_ctx_sweep.sh              # 40k+80k together, then 120k+native
#   bash scripts/run_luna_ctx_sweep.sh 40k 80k      # only these windows
#   bash scripts/run_luna_ctx_sweep.sh native       # a single window
#   PARALLEL=1 bash scripts/run_luna_ctx_sweep.sh   # one job at a time
#
# Each window is its own Harbor job (jobs/lhtb-luna-ctx<window>). Windows run in
# batches of PARALLEL jobs (default 2); the next batch starts when the current one
# has finished. Logs go to jobs/sweep_logs/. Re-running resumes unfinished jobs.
# After each batch, per-turn tables and the sweep table are refreshed:
#   jobs/lhtb-luna-ctx*/token_turns/{turns,summary}.csv
#   jobs/sweep_luna_ctx.csv
set -euo pipefail

cd "$(dirname "$0")/.."

WINDOWS=("$@")
if [[ ${#WINDOWS[@]} -eq 0 ]]; then
  WINDOWS=(40k 80k 120k native)
fi
PARALLEL=${PARALLEL:-2}

for w in "${WINDOWS[@]}"; do
  if [[ ! -f configs/sweep/luna_ctx${w}.yaml ]]; then
    echo "no config for window '$w': configs/sweep/luna_ctx${w}.yaml" >&2
    exit 1
  fi
done

# shellcheck disable=SC1091
source .venv/bin/activate

# Optional local secrets (gitignored): OPENAI_API_KEY=...
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
for w in "${WINDOWS[@]}"; do
  if grep -q '^  type: daytona' "configs/sweep/luna_ctx${w}.yaml"; then
    if [[ -z "${DAYTONA_API_KEY:-}" ]]; then
      echo "DAYTONA_API_KEY is not set (export it or add it to .env)." >&2
      exit 1
    fi
  elif ! docker info >/dev/null 2>&1; then
    echo "Docker is not running. Start Docker Desktop first." >&2
    exit 1
  fi
done

# Keep the same chat after each binary verifier rejection instead of a fresh one.
export HB_CONTINUE_MODE=same_conversation
export HB_VERIFIER_FEEDBACK_MODE=binary
export DOCKER_DEFAULT_PLATFORM=linux/amd64

mkdir -p jobs/sweep_logs

start_job() {
  local w=$1
  local job_dir=jobs/lhtb-luna-ctx${w}
  local log=jobs/sweep_logs/luna_ctx${w}.log
  if [[ -f "$job_dir/config.json" ]]; then
    harbor jobs resume -p "$job_dir" >>"$log" 2>&1 &
  else
    harbor run -c "configs/sweep/luna_ctx${w}.yaml" >>"$log" 2>&1 &
  fi
  echo "$(date '+%F %T') started window=$w pid=$! log=$log"
}

status=0
for ((i = 0; i < ${#WINDOWS[@]}; i += PARALLEL)); do
  batch=("${WINDOWS[@]:i:PARALLEL}")
  pids=()
  for w in "${batch[@]}"; do
    start_job "$w"
    pids+=($!)
  done
  for pid in "${pids[@]}"; do
    wait "$pid" || status=1
  done
  echo "$(date '+%F %T') finished batch: ${batch[*]}"

  for w in "${batch[@]}"; do
    if [[ -d jobs/lhtb-luna-ctx${w} ]]; then
      python scripts/token_turns.py "jobs/lhtb-luna-ctx${w}"
    fi
  done
  python scripts/sweep_summary.py jobs/lhtb-luna-ctx* --out jobs/sweep_luna_ctx.csv || true
done

exit "$status"
