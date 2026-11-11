#!/usr/bin/env bash
# Proxy launcher for Claude Code / Codex jobs. scripts/run_job.sh calls this automatically when a
# config has a `# proxy:` line, so `bash scripts/run_job.sh <config>` is the one entry point;
# running this script directly still works:
#
#   bash scripts/run_harness_job.sh configs/sweep/<job>.yaml
#   nohup caffeinate -ims bash scripts/run_harness_job.sh configs/sweep/<job>.yaml > /dev/null 2>&1 &   # macOS
#   setsid nohup bash scripts/run_harness_job.sh configs/sweep/<job>.yaml > /dev/null 2>&1 &            # Linux
#
# When the config has a `# proxy: {...}` line, starts optlimit.serve first, points the harness at
# it (Claude Code ANTHROPIC_BASE_URL, Codex OPENAI_BASE_URL) and stops it when the job ends.
# Everything else is scripts/run_job.sh (keys, docker/daytona checks, resume, token tables).
#
# The task container must reach the proxy: host.docker.internal on Docker Desktop (macOS),
# this host's first address on Linux; override with OPTLIMIT_PROXY_HOST (an address or a
# public tunnel host for daytona). OPTLIMIT_PROXY_PORT picks the port (default: a free one).
# OPTLIMIT_ANTHROPIC_UPSTREAM / OPTLIMIT_OPENAI_UPSTREAM replace the real APIs (fake_llm_upstream.py).
# Proxy log: jobs/logs/<job>.proxy.jsonl (summarise with analysis/proxy_turns.py).
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG=${1:?usage: run_harness_job.sh <config.yaml>}
JOB_NAME=$(sed -n 's/^job_name: *//p' "$CONFIG")
MODEL=$(sed -n 's/^ *model_name: *//p' "$CONFIG" | head -1)
PROXY_JSON=$(sed -n 's/^# proxy: *//p' "$CONFIG")
mkdir -p jobs/logs

if [[ -z "$PROXY_JSON" ]]; then
  exec bash scripts/run_job.sh "$CONFIG"
fi

VENV=.venv
HARBOR_PROFILE=$(sed -n 's/^# harbor: *//p' "$CONFIG" | head -1)
case "$HARBOR_PROFILE" in 0.25*) VENV=.venv-h25 ;; esac
[[ -f $VENV/bin/activate ]] || { echo "missing $VENV: see setup.sh --profile" >&2; exit 1; }
# shellcheck disable=SC1090,SC1091
source "$VENV/bin/activate"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
python -c "import httpx, starlette, uvicorn" 2>/dev/null \
  || { echo "proxy dependencies missing: uv pip install -e '.[proxy]'" >&2; exit 1; }

if [[ -n "${OPTLIMIT_PROXY_HOST:-}" ]]; then
  HOST=$OPTLIMIT_PROXY_HOST
elif grep -q '^  type: daytona' "$CONFIG"; then
  echo "daytona sandboxes cannot reach a proxy on this host; set OPTLIMIT_PROXY_HOST to a public tunnel host" >&2
  exit 1
elif [[ "$(uname)" == Darwin ]]; then
  HOST=host.docker.internal
else
  HOST=$(hostname -I | awk '{print $1}')
fi
PORT=${OPTLIMIT_PROXY_PORT:-$(python -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1])')}

SPEC_FILE=jobs/logs/$JOB_NAME.proxy-spec.json
python - "$PROXY_JSON" "$SPEC_FILE" <<'PY'
import json, sys
json.dump(json.loads(sys.argv[1])["spec"], open(sys.argv[2], "w"))
PY
read -r TRIGGER LOW HIGH MINWIN ORPHAN < <(python - "$PROXY_JSON" <<'PY'
import json, sys
p = json.loads(sys.argv[1])
print(p["trigger"], p.get("low_cap", 50000), p.get("high", 950000), p.get("min_window_turns", 0),
      int(bool(p.get("attach_orphan_summaries"))))
PY
)
ARGS=(--host 0.0.0.0 --port "$PORT" --trigger "$TRIGGER" --spec-file "$SPEC_FILE"
      --log "jobs/logs/$JOB_NAME.proxy.jsonl" --signal-low-cap "$LOW" --signal-high "$HIGH"
      --min-window-turns "$MINWIN")
[[ "$ORPHAN" == 1 ]] && ARGS+=(--attach-orphan-summaries)
# upstream overrides (e.g. scripts/fake_llm_upstream.py for a keyless check of the trigger path)
[[ -n "${OPTLIMIT_ANTHROPIC_UPSTREAM:-}" ]] && ARGS+=(--anthropic-upstream "$OPTLIMIT_ANTHROPIC_UPSTREAM")
[[ -n "${OPTLIMIT_OPENAI_UPSTREAM:-}" ]] && ARGS+=(--openai-upstream "$OPTLIMIT_OPENAI_UPSTREAM")
# per-request text dumps (classifying a harness's compaction prompt): OPTLIMIT_PROXY_DUMP=1
[[ -n "${OPTLIMIT_PROXY_DUMP:-}" ]] && ARGS+=(--dump-dir "jobs/logs/$JOB_NAME.dumps")
case "$MODEL" in
  anthropic/*) ARGS+=(--model-rewrite "$MODEL=${MODEL#anthropic/}")
               export ANTHROPIC_BASE_URL="http://$HOST:$PORT"
               # host-side harnesses (stock terminus-2 through litellm) read this name
               export ANTHROPIC_API_BASE="http://$HOST:$PORT" ;;
  openai/*)    export OPENAI_BASE_URL="http://$HOST:$PORT/v1" ;;
  *) echo "run_harness_job: unsupported model prefix in $MODEL" >&2; exit 1 ;;
esac

# Harbor >= 0.20 enforces per-task egress allowlists: let the agent container reach the proxy.
# (--allow-environment-host covers agent setup in tasks whose whole environment is allowlisted.)
case "$HARBOR_PROFILE" in 0.25*) export HARBOR_EXTRA_ARGS="${HARBOR_EXTRA_ARGS:-} --allow-agent-host $HOST --allow-environment-host $HOST" ;; esac

python -m optlimit.serve "${ARGS[@]}" >>"jobs/logs/$JOB_NAME.proxy.out" 2>&1 &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$PORT/__optlimit__/status" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/__optlimit__/status" >/dev/null \
  || { echo "proxy did not start; see jobs/logs/$JOB_NAME.proxy.out" >&2; exit 1; }
echo "$(date '+%F %T') optlimit proxy pid $PROXY_PID on :$PORT (trigger $TRIGGER), harness -> http://$HOST:$PORT" \
  | tee -a "jobs/logs/$JOB_NAME.log"

status=0
OPTLIMIT_PROXY_ACTIVE=1 bash scripts/run_job.sh "$CONFIG" || status=$?
if [[ -f analysis/proxy_turns.py ]]; then
  python analysis/proxy_turns.py "jobs/logs/$JOB_NAME.proxy.jsonl" --prices-from "$SPEC_FILE" || true
fi
exit "$status"
