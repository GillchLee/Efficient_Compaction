#!/usr/bin/env bash
# Rebuild the experiment workspace from scratch:
#   ./LHTB at the pinned commit + Harbor patches (patches/NN-*.patch, in order) + ./.venv with the patched Harbor and optlimit.
#
#   bash setup.sh                 # LHTB -> ./LHTB, venv -> ./.venv
#
# Env overrides: LHTB_URL (clone source), LHTB_DIR (default ./LHTB), PY (python for the
# venv, default 3.12), SKIP_LFS=1 (skip `git lfs pull`; automatic when git-lfs is missing).
# Nothing is copied into LHTB: jobs run from this directory and configs point at
# ./LHTB/tasks (LHTB 1.0) or ./LHTB/tasks-2.0 (LHTB 2.0).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
LHTB_DIR=${LHTB_DIR:-$HERE/LHTB}
LHTB_URL=${LHTB_URL:-https://github.com/zli12321/LHTB}
LHTB_COMMIT=7ca0cacea9bf6a0694eaad7306bed794eaaaabce
PATCH_DIR=$HERE/patches
PY=${PY:-3.12}

for cmd in git uv; do
  command -v "$cmd" >/dev/null || { echo "missing: $cmd" >&2; exit 1; }
done
if [[ "${SKIP_LFS:-0}" != 1 ]] && ! git lfs version >/dev/null 2>&1; then
  echo "git-lfs not installed: skipping LFS pull (only some task assets need it)" >&2
  SKIP_LFS=1
fi

# 1. LHTB at the commit the experiments ran on (shallow: the commit is the current main)
if [[ ! -d "$LHTB_DIR/.git" ]]; then
  git clone --depth 1 "$LHTB_URL" "$LHTB_DIR" || true
fi
if [[ "${SKIP_LFS:-0}" == 1 ]]; then
  # a global `git lfs install` makes checkout fail without the binary: keep LFS pointers as files
  git -C "$LHTB_DIR" config filter.lfs.required false
  git -C "$LHTB_DIR" config filter.lfs.process ""
  git -C "$LHTB_DIR" config filter.lfs.smudge cat
  git -C "$LHTB_DIR" config filter.lfs.clean cat
fi
if ! git -C "$LHTB_DIR" rev-parse --verify --quiet "$LHTB_COMMIT^{commit}" >/dev/null; then
  git -C "$LHTB_DIR" fetch --depth 1 origin "$LHTB_COMMIT"
fi
git -C "$LHTB_DIR" checkout --quiet --force "$LHTB_COMMIT"
if [[ "${SKIP_LFS:-0}" != 1 ]]; then
  git -C "$LHTB_DIR" lfs install --local
  git -C "$LHTB_DIR" lfs pull
fi

# 2. Harbor patches, in order (patches/NN-*.patch; each one is skipped when already applied)
for PATCH in "$PATCH_DIR"/[0-9][0-9]-*.patch; do
  if git -C "$LHTB_DIR" apply --check "$PATCH" 2>/dev/null; then
    git -C "$LHTB_DIR" apply "$PATCH"
    echo "applied $(basename "$PATCH")"
  elif git -C "$LHTB_DIR" apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "already applied: $(basename "$PATCH")"
  else
    echo "patch does not apply cleanly to $LHTB_DIR: $(basename "$PATCH")" >&2
    exit 1
  fi
done

# 3. venv: patched Harbor (+ Daytona SDK), optlimit (this repo), pytest
cd "$HERE"
if [[ ! -d .venv ]]; then
  uv venv -p "$PY" .venv
fi
uv pip install --python .venv/bin/python -e "$LHTB_DIR/harbor[daytona]" -e . pytest pytest-asyncio

# 4. Secrets file (never committed)
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "created .env: fill in the keys"
fi

mkdir -p jobs/logs
echo "done. next: edit .env, then e.g."
echo "  python configs/make_config.py --model anthropic/claude-sonnet-5-5 --limit 80000 --tasks seismo_cross_correlation_lag__01 --hours 0.25"
echo "  bash scripts/run_job.sh configs/sweep/<job>.yaml"
