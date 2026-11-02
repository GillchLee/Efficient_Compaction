#!/usr/bin/env bash
# Rebuild the experiment workspace from scratch:
#   LHTB at the pinned commit + Harbor patch + this repo's configs and scripts + venv.
#
#   bash setup.sh [target_dir]        # default: ./LHTB next to this file
#
# Env overrides: LHTB_URL (clone source), SKIP_LFS=1 (skip `git lfs pull`).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
TARGET=${1:-$HERE/LHTB}
LHTB_URL=${LHTB_URL:-https://github.com/zli12321/LHTB}
LHTB_COMMIT=7ca0cacea9bf6a0694eaad7306bed794eaaaabce
PATCH=$HERE/patches/harbor-token-and-summary-tracking.patch

for cmd in git uv; do
  command -v "$cmd" >/dev/null || { echo "missing: $cmd" >&2; exit 1; }
done
if [[ "${SKIP_LFS:-0}" != 1 ]] && ! command -v git-lfs >/dev/null; then
  echo "missing: git-lfs (brew install git-lfs), or set SKIP_LFS=1" >&2
  exit 1
fi

# 1. LHTB at the commit the experiments ran on
if [[ ! -d "$TARGET/.git" ]]; then
  git clone "$LHTB_URL" "$TARGET"
fi
cd "$TARGET"
git checkout --quiet "$LHTB_COMMIT"
if [[ "${SKIP_LFS:-0}" != 1 ]]; then
  git lfs install --local
  git lfs pull
fi

# 2. Harbor patch: per-turn cache write, short-summary tracking
if git apply --check "$PATCH" 2>/dev/null; then
  git apply "$PATCH"
  echo "applied $(basename "$PATCH")"
elif git apply --reverse --check "$PATCH" 2>/dev/null; then
  echo "patch already applied"
else
  echo "patch does not apply cleanly to $TARGET" >&2
  exit 1
fi

# 3. Configs and scripts
mkdir -p configs/sweep configs/smoke configs/examples
cp "$HERE"/configs/sweep/*.yaml configs/sweep/
cp "$HERE"/configs/smoke/*.yaml configs/smoke/
cp "$HERE"/configs/examples/*.yaml configs/examples/
for f in "$HERE"/scripts/*; do
  cp "$f" scripts/
  chmod +x "scripts/$(basename "$f")"
done

# 4. Python 3.12 venv with the patched Harbor (+ Daytona SDK)
if [[ ! -d .venv ]]; then
  uv venv -p 3.12 .venv
fi
uv pip install --python .venv/bin/python -e 'harbor[daytona]'

# 5. Secrets file (never committed)
if [[ ! -f .env ]]; then
  cp "$HERE/.env.example" .env
  chmod 600 .env
  echo "created $TARGET/.env: fill in the keys"
fi

echo "done: $TARGET"
echo "next: edit .env, then e.g. bash scripts/run_job.sh configs/smoke/luna_ctx6k_smoke.yaml"
