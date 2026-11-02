#!/usr/bin/env bash
# Run configs/examples/claude_long3.yaml with same-conversation continuation.
#
#   bash scripts/run_claude_long3.sh [extra harbor run args...]
#
# Re-running resumes the existing job (finished trials are kept). To resume
# explicitly:  harbor jobs resume -p jobs/lhtb-claude-long3
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG=configs/examples/claude_long3.yaml
JOB_DIR=jobs/lhtb-claude-long3

# shellcheck disable=SC1091
source .venv/bin/activate

# Optional local secrets (gitignored): ANTHROPIC_API_KEY=...
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ANTHROPIC_API_KEY is not set in this shell." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop first." >&2
  exit 1
fi

# Keep the same chat after each binary verifier rejection instead of a fresh one.
export HB_CONTINUE_MODE=same_conversation
export HB_VERIFIER_FEEDBACK_MODE=binary
export DOCKER_DEFAULT_PLATFORM=linux/amd64

if [[ -f "$JOB_DIR/config.json" ]]; then
  harbor jobs resume -p "$JOB_DIR"
else
  harbor run -c "$CONFIG" "$@"
fi

python scripts/token_turns.py "$JOB_DIR"
