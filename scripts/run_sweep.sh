#!/usr/bin/env bash
# Run several job configs, PARALLEL at a time, then refresh the per-turn tables and the sweep table.
#
#   bash scripts/run_sweep.sh configs/sweep/luna_ctx*.yaml           # 2 at a time (default)
#   PARALLEL=1 bash scripts/run_sweep.sh configs/sweep/sonnet_*.yaml # one job at a time
#
# Each config is its own Harbor job (jobs/<job_name>) started through scripts/run_job.sh,
# so re-running resumes unfinished jobs. Logs: jobs/logs/<job_name>.log.
# After each batch:  jobs/<job>/token_turns/{turns,summary,submissions}.csv  and
# jobs/sweep_summary.csv over every job given.
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIGS=("$@")
if [[ ${#CONFIGS[@]} -eq 0 ]]; then
  echo "usage: run_sweep.sh <config.yaml>..." >&2
  exit 1
fi
PARALLEL=${PARALLEL:-2}
for c in "${CONFIGS[@]}"; do
  [[ -f "$c" ]] || { echo "no such config: $c" >&2; exit 1; }
done

job_of() { sed -n 's/^job_name: *//p' "$1"; }

status=0
for ((i = 0; i < ${#CONFIGS[@]}; i += PARALLEL)); do
  batch=("${CONFIGS[@]:i:PARALLEL}")
  pids=()
  for c in "${batch[@]}"; do
    bash scripts/run_job.sh "$c" &
    pids+=($!)
    echo "$(date '+%F %T') started $(job_of "$c") pid=$!"
    sleep 5
  done
  for pid in "${pids[@]}"; do
    wait "$pid" || status=1
  done
  echo "$(date '+%F %T') finished batch: $(for c in "${batch[@]}"; do job_of "$c"; done | xargs)"
  # shellcheck disable=SC1091
  source .venv/bin/activate
  dirs=()
  for c in "${CONFIGS[@]}"; do d=jobs/$(job_of "$c"); [[ -d "$d" ]] && dirs+=("$d"); done
  python analysis/sweep_summary.py "${dirs[@]}" --out jobs/sweep_summary.csv || true
done

exit "$status"
