#!/usr/bin/env bash
# One command for a local vLLM model: check the server, measure its prices (once),
# write the job config and run it with scripts/run_job.sh.
#
#   bash scripts/run_local_vllm.sh --model Qwen3-32B --api-base http://localhost:8000/v1 --context 131072 \
#        --limit 40000 --tasks spot-scheduler-traces --hours 1
#   bash scripts/run_local_vllm.sh --model Qwen3-32B --api-base http://localhost:8000/v1 --context 131072 \
#        --formula terminus2-gpt-6-luna-1.0 --tasks spot-scheduler-traces 2048 --hours 3 --concurrent 2
#
#   --model NAME        served model name (vllm serve ... --served-model-name NAME); becomes hosted_vllm/NAME
#   --api-base URL      vLLM OpenAI endpoint, e.g. http://host:8000/v1 (default: HOSTED_VLLM_API_BASE from .env)
#   --context N         model context window in tokens (required; fills Harbor model_info)
#   --limit N | --formula PRIOR | --native
#                       fixed compaction limit, dynamic L* from configs/priors.json[PRIOR], or stock terminus-2
#   --tasks-dir DIR     default ./LHTB/tasks (LHTB 1.0); ./LHTB/tasks-2.0 for the 2.0 additions
#   --tasks A B ...     task names (default: every task in --tasks-dir)
#   --hours H           agent budget per task (default 3)
#   --concurrent N      trials at once (default 1; keep the vLLM KV cache in mind)
#   --job-name NAME     default <model>_<condition>
#   --measure           re-measure prices even if the row exists
#   --dry-run           stop after writing the config
#   Anything after "--" is passed to configs/make_config.py (e.g. -- --set g_prior_weight=0 --verifier-feedback false)
#
# The server must run with --enable-prefix-caching so usage reports cached_tokens.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && { set -a; source .env; set +a; }

MODEL=""; API_BASE="${HOSTED_VLLM_API_BASE:-}"; CONTEXT=""; COND=(); TASKS_DIR=./LHTB/tasks; TASKS=()
HOURS=3; CONCURRENT=1; JOB=""; MEASURE=0; DRY=0; EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL=$2; shift 2 ;;
    --api-base) API_BASE=$2; shift 2 ;;
    --context) CONTEXT=$2; shift 2 ;;
    --limit) COND=(--limit "$2"); CONDNAME="l$(( $2 / 1000 ))k"; shift 2 ;;
    --formula) COND=(--formula "$2"); CONDNAME=formula; PRIOR=$2; shift 2 ;;
    --native) COND=(--limit native); CONDNAME=native; shift ;;
    --tasks-dir) TASKS_DIR=$2; shift 2 ;;
    --tasks) shift; while [[ $# -gt 0 && "$1" != --* ]]; do TASKS+=("$1"); shift; done ;;
    --hours) HOURS=$2; shift 2 ;;
    --concurrent) CONCURRENT=$2; shift 2 ;;
    --job-name) JOB=$2; shift 2 ;;
    --measure) MEASURE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$MODEL" && -n "$CONTEXT" && ${#COND[@]} -gt 0 ]] || { sed -n '2,24p' "$0" >&2; exit 2; }
[[ -n "$API_BASE" ]] || { echo "--api-base or HOSTED_VLLM_API_BASE (.env) is required" >&2; exit 2; }
PY=${PY:-.venv/bin/python}

if ! curl -sf -m 10 -H "Authorization: Bearer ${HOSTED_VLLM_API_KEY:-EMPTY}" "${API_BASE%/}/models" | grep -q "\"$MODEL\""; then
  echo "model '$MODEL' not served at ${API_BASE%/}/models:" >&2
  curl -s -m 10 -H "Authorization: Bearer ${HOSTED_VLLM_API_KEY:-EMPTY}" "${API_BASE%/}/models" >&2 || true
  exit 1
fi

PRICES="vllm-$(echo "$MODEL" | tr '[:upper:]/' '[:lower:]-')"
if [[ "${CONDNAME}" == formula ]]; then
  if [[ $MEASURE -eq 1 ]] || ! "$PY" -c "import json,sys; sys.exit('$PRICES' not in json.load(open('configs/prices.json'))['models'])"; then
    echo "measuring prices for $MODEL -> configs/prices.json[$PRICES]"
    "$PY" scripts/measure_vllm_prices.py --api-base "$API_BASE" --model "$MODEL" --name "$PRICES"
  fi
  COND+=(--prices "$PRICES")
fi

JOB=${JOB:-"$(echo "$MODEL" | tr '[:upper:]/' '[:lower:]-')_${CONDNAME}"}
CONFIG=configs/sweep/$JOB.yaml
"$PY" configs/make_config.py --model "hosted_vllm/$MODEL" --api-base "$API_BASE" --context-window "$CONTEXT" \
  "${COND[@]}" --tasks-dir "$TASKS_DIR" ${TASKS[@]:+--tasks "${TASKS[@]}"} --hours "$HOURS" --concurrent "$CONCURRENT" \
  --job-name "$JOB" --out "$CONFIG" "${EXTRA[@]}"
[[ $DRY -eq 1 ]] && { echo "config written: $CONFIG (dry run)"; exit 0; }
exec bash scripts/run_job.sh "$CONFIG"
