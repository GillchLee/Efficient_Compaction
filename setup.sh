#!/usr/bin/env bash
# Rebuild an experiment environment from scratch. One profile per Harbor base (they cannot share a venv):
#
#   bash setup.sh                       # profile lhtb: ./LHTB at the pinned commit + patches/lhtb-7ca0cac/NN-*.patch
#                                       # (in order) + ./.venv with the patched Harbor (0.7 fork) and optlimit
#   bash setup.sh --profile harbor25    # stock Harbor (HARBOR_VERSION, default 0.25.0) + optlimit[proxy] in ./.venv-h25:
#                                       # benchmarks that need newer task features (SWE-Marathon, FrontierSWE v2)
#
# configs/benchmarks.yaml says which profile a benchmark needs (`harbor:`); make_config.py records it
# as a `# harbor:` line and scripts/run_job.sh activates the matching venv.
#
# Env overrides: LHTB_URL (clone source), LHTB_DIR (default ./LHTB), PY (python for the
# venv, default 3.12), SKIP_LFS=1 (skip `git lfs pull`; automatic when git-lfs is missing).
# Nothing is copied into LHTB: jobs run from this directory and configs point at
# ./LHTB/tasks (LHTB 1.0) or ./LHTB/tasks-2.0 (LHTB 2.0).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PROFILE=lhtb
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE=${2:?--profile lhtb|harbor25}; shift 2 ;;
    --profile=*) PROFILE=${1#*=}; shift ;;
    *) echo "unknown argument: $1 (usage: setup.sh [--profile lhtb|harbor25])" >&2; exit 1 ;;
  esac
done

if [[ "$PROFILE" == harbor25 ]]; then
  # Stock Harbor from PyPI: no LHTB checkout, no patches. Claude Code / Codex / stock terminus-2 run
  # behind the optlimit proxy (docs/other_harnesses.md), so nothing in Harbor needs changing.
  command -v uv >/dev/null || { echo "missing: uv" >&2; exit 1; }
  cd "$HERE"
  [[ -d .venv-h25 ]] || uv venv -p "${PY:-3.12}" .venv-h25
  uv pip install --python .venv-h25/bin/python "harbor==${HARBOR_VERSION:-0.25.0}" -e ".[proxy]" pytest pytest-asyncio
  # Stock terminus-2 apt-installs tmux at trial start with a 120 s per-command ceiling; with 4 trials
  # starting at once on a slow apt mirror that failed 2-3 trials per job (SWE-Marathon 2026-11-11,
  # "Failed to start tmux session"). Raise it to 300 s / 330 s total, still under Harbor's 360 s
  # agent-setup timeout.
  TS=$(.venv-h25/bin/python -c "import harbor.agents.terminus_2.tmux_session as m; print(m.__file__)")
  sed -i 's/^    _TOOL_INSTALL_TIMEOUT_SEC = 120$/    _TOOL_INSTALL_TIMEOUT_SEC = 300  # optlimit setup.sh: slow apt mirrors (was 120)/; s/^    _TOOL_INSTALL_BUDGET_SEC = 240$/    _TOOL_INSTALL_BUDGET_SEC = 330  # optlimit setup.sh: under the 360 s agent-setup timeout (was 240)/' "$TS"
  # Wait (<= 60 s) for the egress sidecar's DNS before apt: the task container shares the sidecar's
  # network namespace and trials starting together lost apt to "Could not resolve" (2026-11-12).
  sed -i 's|"apt-get": f"DEBIAN_FRONTEND=noninteractive apt-get update \&\& |"apt-get": f"for i in $(seq 1 30); do getent hosts archive.ubuntu.com >/dev/null 2>\&1 \|\| getent hosts deb.debian.org >/dev/null 2>\&1 \&\& break; sleep 2; done; DEBIAN_FRONTEND=noninteractive apt-get update \&\& |' "$TS"
  if [[ ! -f .env ]]; then cp .env.example .env; chmod 600 .env; echo "created .env: fill in the keys"; fi
  mkdir -p jobs/logs data
  echo "done (profile harbor25, $(.venv-h25/bin/harbor --version 2>/dev/null | tail -1)). next e.g."
  echo "  git clone --depth 1 https://github.com/abundant-ai/swe-marathon data/swe-marathon"
  echo "  .venv-h25/bin/python configs/make_config.py --harness claude-code --model anthropic/claude-haiku-5-5 --proxy-fixed 40k --benchmark swe_marathon --tasks zstd-decoder --hours 1"
  exit 0
elif [[ "$PROFILE" != lhtb ]]; then
  echo "unknown profile: $PROFILE (lhtb | harbor25)" >&2; exit 1
fi
LHTB_DIR=${LHTB_DIR:-$HERE/LHTB}
LHTB_URL=${LHTB_URL:-https://github.com/zli12321/LHTB}
LHTB_COMMIT=7ca0cacea9bf6a0694eaad7306bed794eaaaabce
PATCH_DIR=$HERE/patches/lhtb-7ca0cac
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

# 2. Harbor patches, in order (patches/lhtb-7ca0cac/NN-*.patch; each one is skipped when already applied)
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

# 3. venv: patched Harbor (+ Daytona SDK), optlimit with the proxy extra (Claude Code / Codex), pytest
cd "$HERE"
if [[ ! -d .venv ]]; then
  uv venv -p "$PY" .venv
fi
uv pip install --python .venv/bin/python -e "$LHTB_DIR/harbor[daytona]" -e ".[proxy]" pytest pytest-asyncio

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
