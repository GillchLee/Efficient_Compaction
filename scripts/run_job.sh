#!/usr/bin/env bash
# Run (or resume) one Harbor job from a config.
#
#   bash scripts/run_job.sh configs/sweep/<job>.yaml
#   setsid nohup bash scripts/run_job.sh configs/sweep/<job>.yaml > /dev/null 2>&1 &   # detached (Linux)
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
API_BASE=$(sed -n 's/^ *api_base: *//p' "$CONFIG" | head -1)
case "$MODEL" in
  anthropic/*) [[ -n "${ANTHROPIC_API_KEY:-}" ]] || { echo "ANTHROPIC_API_KEY is not set (.env)" >&2; exit 1; } ;;
  openai/*)    [[ -n "${OPENAI_API_KEY:-}" ]]    || { echo "OPENAI_API_KEY is not set (.env)" >&2; exit 1; } ;;
  dashscope/*) # Alibaba Cloud Model Studio (Qwen); DASHSCOPE_API_BASE selects the region (.env)
    [[ -n "${DASHSCOPE_API_KEY:-}" ]] || { echo "DASHSCOPE_API_KEY is not set (.env)" >&2; exit 1; } ;;
  hosted_vllm/*)
    # local OpenAI-compatible server (vLLM): endpoint from the config's api_base or .env
    API_BASE=${API_BASE:-${HOSTED_VLLM_API_BASE:-}}
    [[ -n "$API_BASE" ]] || { echo "hosted_vllm/ needs api_base in $CONFIG (make_config.py --api-base) or HOSTED_VLLM_API_BASE (.env)" >&2; exit 1; }
    if ! curl -sf -m 10 -H "Authorization: Bearer ${HOSTED_VLLM_API_KEY:-EMPTY}" "${API_BASE%/}/models" >/dev/null; then
      echo "vLLM server not reachable at ${API_BASE%/}/models" >&2; exit 1
    fi ;;
esac
if grep -q '^  type: daytona' "$CONFIG"; then
  [[ -n "${DAYTONA_API_KEY:-}" ]] || { echo "DAYTONA_API_KEY is not set (.env)" >&2; exit 1; }
elif ! docker info >/dev/null 2>&1; then
  echo "Docker is not running." >&2
  exit 1
fi

export HB_CONTINUE_MODE=${HB_CONTINUE_MODE-same_conversation}
export HB_VERIFIER_FEEDBACK_MODE=binary   # kept for older configs; Harbor 7ca0cac does not read it
export DOCKER_DEFAULT_PLATFORM=linux/amd64
# Trial-loop options recorded by make_config.py as "# env: KEY=VALUE ..." (HB_VERIFIER_FEEDBACK,
# HB_CONTINUE_MODE, HB_PROCESS_REWARD*): the config wins over the shell so a job dir reproduces.
for kv in $(sed -n 's/^# env: *//p' "$CONFIG"); do
  case "$kv" in
    HB_[A-Z_]*=*) export "$kv" ;;
    *) echo "ignoring config env entry: $kv" >&2 ;;
  esac
done

mkdir -p jobs/logs

# Host memory guard. Harbor caps every task container at task.toml memory_mb (compose
# deploy.resources.limits), so a trial cannot take more than that, but the host must still
# have it free or the kernel OOM killer picks a victim. Need = n_concurrent_trials x the
# largest memory_mb among the selected tasks + HB_MEM_MARGIN_MB for harbor/verifier.
# HB_MEM_CHECK=0 skips it; HB_MEM_WAIT_SEC=0 fails instead of waiting (default: wait 24 h).
if [[ "${HB_MEM_CHECK:-1}" != 0 ]]; then
  NEED_MB=$(python3 - "$CONFIG" <<'PY'
import re, sys, pathlib
try:
    import tomllib
except ImportError:
    tomllib = None
import yaml
cfg = yaml.safe_load(open(sys.argv[1]))
conc = int(cfg.get("n_concurrent_trials", 1) or 1)
mems = []
for ds in cfg.get("datasets", []):
    root = pathlib.Path(ds.get("path", "."))
    names = ds.get("task_names") or [d.name for d in root.iterdir() if (d / "task.toml").exists()]
    for n in names:
        t = root / n / "task.toml"
        if not t.exists():
            continue
        txt = t.read_text()
        m = re.search(r"^memory_mb\s*=\s*(\d+)", txt, re.M)
        mems.append(int(m.group(1)) if m else 4096)
print(min(conc, max(1, len(mems))) * (max(mems) if mems else 4096))
PY
  )
  NEED_MB=$(( NEED_MB + ${HB_MEM_MARGIN_MB:-2048} ))
  avail_mb() { awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo; }
  waited=0
  while (( $(avail_mb) < NEED_MB )); do
    if (( waited == 0 )); then
      echo "$(date '+%F %T') host MemAvailable $(avail_mb) MB < needed $NEED_MB MB; waiting (HB_MEM_WAIT_SEC=${HB_MEM_WAIT_SEC:-86400})" | tee -a "$LOG"
    fi
    if (( waited >= ${HB_MEM_WAIT_SEC:-86400} )); then
      echo "$(date '+%F %T') still $(avail_mb) MB available after ${waited}s; giving up" | tee -a "$LOG"; exit 1
    fi
    sleep 60; waited=$(( waited + 60 ))
    (( waited % 600 == 0 )) && echo "$(date '+%F %T') MemAvailable $(avail_mb) MB, need $NEED_MB MB" >>"$LOG"
  done
  echo "$(date '+%F %T') memory ok: MemAvailable $(avail_mb) MB >= needed $NEED_MB MB" | tee -a "$LOG"
fi

echo "$(date '+%F %T') start $JOB_NAME ($CONFIG, HB_CONTINUE_MODE=$HB_CONTINUE_MODE, HB_VERIFIER_FEEDBACK=${HB_VERIFIER_FEEDBACK-true})" | tee -a "$LOG"

# terminus-2 pipes the tmux pane into agent/terminus_2.pane for debugging only (the agent reads
# the screen with capture-pane, the trajectory holds the observations). A command that floods
# the terminal can grow it without bound (58 GB seen): keep it under PANE_MAX_MB.
PANE_MAX_MB=${PANE_MAX_MB:-512}
(
  while sleep 60; do
    find "$JOB_DIR" -name terminus_2.pane -size +"${PANE_MAX_MB}M" -print 2>/dev/null | while read -r f; do
      echo "$(date '+%F %T') trimming $f ($(du -m "$f" | cut -f1) MB)" >>"$LOG"
      if ! truncate -s "${PANE_MAX_MB}M" "$f" 2>/dev/null; then
        # tmux creates the file as root inside the container (bind mount /logs/agent), so the
        # host user cannot truncate it: do it inside the trial's compose container instead.
        trial=$(basename "$(dirname "$(dirname "$f")")")
        cname="$(printf '%s' "$trial" | tr '[:upper:]' '[:lower:]')-main-1"
        docker exec "$cname" truncate -s "${PANE_MAX_MB}M" /logs/agent/terminus_2.pane >>"$LOG" 2>&1 \
          || echo "$(date '+%F %T') trim failed for $f (container $cname)" >>"$LOG"
      fi
    done
  done
) &
TRIM_PID=$!
trap 'kill $TRIM_PID 2>/dev/null' EXIT
status=0
if [[ -f "$JOB_DIR/config.json" ]]; then
  harbor jobs resume -p "$JOB_DIR" >>"$LOG" 2>&1 || status=$?
else
  harbor run -c "$CONFIG" >>"$LOG" 2>&1 || status=$?
fi
echo "$(date '+%F %T') finished $JOB_NAME (exit $status)" | tee -a "$LOG"

python analysis/token_turns.py "$JOB_DIR"
exit "$status"
