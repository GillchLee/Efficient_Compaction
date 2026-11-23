#!/usr/bin/env bash
# One-time preparation of a Slurm cluster for the LHTB 2.0 runs (run on a login node, in the repo root's
# checkout; needs internet). Safe to rerun: every step is skipped when already done.
#
#   cp scripts/slurm/lhtb_par.conf.example scripts/slurm/lhtb_par.conf   # edit ENV_TYPE, TASKS, ...
#   scp <this host>:.env .env                                             # OPENAI_API_KEY (never commit it)
#   bash scripts/slurm/setup_server.sh              # Harbor venv + LHTB tasks + checks
#   PULL=1 bash scripts/slurm/setup_server.sh       # singularity: also convert the task images now
#                                                   # (needed when compute nodes cannot reach Docker Hub)
# Then check a COMPUTE node once (they often differ from the login node):
#   srun -p <partition> -c 2 --mem 8G -t 10 bash scripts/slurm/setup_server.sh --check-only
set -uo pipefail
cd "$(dirname "$0")/../.."
CONF=${CONF:-scripts/slurm/lhtb_par.conf}
[[ -f $CONF ]] || { echo "missing $CONF: cp scripts/slurm/lhtb_par.conf.example $CONF and edit it" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONF"
ENV_TYPE=${ENV_TYPE:-singularity}; HARBOR=${HARBOR:-0.25}
[[ $HARBOR == 0.25 ]] && VENV=.venv-h25 || VENV=.venv
[[ -n "${PRE_CMD:-}" ]] && eval "$PRE_CMD"
fails=0
pass() { echo "PASS  $*"; }
warn() { echo "WARN  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }

if [[ ${1:-} != --check-only ]]; then
  # 1. Harbor + optlimit, and the LHTB checkout (tasks) at the pinned commit
  command -v uv >/dev/null || { echo "missing: uv (curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2; exit 1; }
  if [[ $HARBOR == 0.25 ]]; then
    [[ -x .venv-h25/bin/harbor ]] && echo ".venv-h25 already set up" \
      || bash setup.sh --profile harbor25 || { echo "setup.sh failed" >&2; exit 1; }
    if [[ ! -d LHTB/tasks-2.0 ]]; then
      COMMIT=$(sed -n 's/^LHTB_COMMIT=//p' setup.sh); URL=${LHTB_URL:-https://github.com/zli12321/LHTB}
      # tasks only (small files): LFS assets are not needed by the 2.0 reconstruction tasks
      GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 "$URL" LHTB || { echo "cannot clone $URL" >&2; exit 1; }
      git -C LHTB rev-parse --verify --quiet "$COMMIT^{commit}" >/dev/null || git -C LHTB fetch --depth 1 origin "$COMMIT"
      GIT_LFS_SKIP_SMUDGE=1 git -C LHTB checkout --quiet --force "$COMMIT" && echo "LHTB at ${COMMIT:0:7}"
    fi
  elif [[ -x .venv/bin/harbor ]] && grep -q singularity_fakeroot LHTB/harbor/src/harbor/environments/singularity/singularity.py 2>/dev/null; then
    echo ".venv and ./LHTB already set up"
  else
    bash setup.sh --profile lhtb || { echo "setup.sh failed" >&2; exit 1; }   # fork + patches 01-06
  fi

  # 2a. docker on the fork: task copies with the container network enabled (the agent must reach the proxy
  #     and install Codex; Harbor 0.25 ignores the tasks' `network = false`)
  if [[ $ENV_TYPE == docker && $HARBOR == fork && ! -d data/lhtb_v2 ]]; then
    mkdir -p data && cp -r LHTB/tasks-2.0 data/lhtb_v2
    sed -i -E 's/^(network|allow_internet) = false/\1 = true/' data/lhtb_v2/*/task.toml
    echo "data/lhtb_v2: $(ls -d data/lhtb_v2/*/ | wc -l) task copies with network enabled"
  fi

  # 2b. singularity: convert the task images now (file names as Harbor's cache expects them)
  if [[ $ENV_TYPE == singularity && -n "${PULL:-}" ]]; then
    CACHE=${SIF_CACHE:-./data/sif-cache}; mkdir -p "$CACHE"
    export APPTAINER_CACHEDIR=${APPTAINER_CACHEDIR:-$PWD/data/sif-cache/oci} SINGULARITY_CACHEDIR=${SINGULARITY_CACHEDIR:-$PWD/data/sif-cache/oci}
    if [[ ${TASKS:-all} == all ]]; then TASKS=$(cd LHTB/tasks-2.0 && for d in */; do [[ -f $d/reconstruction.json ]] && echo "${d%/}"; done | tr '\n' ' '); fi
    for t in $TASKS; do
      img=$(sed -n 's/^docker_image = "\(.*\)"/\1/p' "LHTB/tasks-2.0/$t/task.toml" | head -1)
      [[ -n $img ]] || { warn "$t: no docker_image"; continue; }
      [[ $img == *:* ]] || img=$img:latest
      sif=$CACHE/$(echo "$img" | sed 's#/#_#g; s#:#_#g').sif
      if [[ -f $sif ]]; then echo "have $t"; continue; fi
      singularity -q pull "$sif.tmp" "docker://$img" && mv "$sif.tmp" "$sif" && echo "pulled $t ($(du -h "$sif" | cut -f1))" \
        || { rm -f "$sif.tmp"; fail "pull failed: $t ($img) - Docker Hub rate limit? apptainer registry login -u <user> docker://docker.io"; }
    done
  fi
fi

# 3. checks (login node, and a compute node through srun ... --check-only)
echo "--- checks on $(hostname) (env $ENV_TYPE, harbor $HARBOR)"
[[ -x $VENV/bin/harbor ]] && pass "harbor ($HARBOR): $($VENV/bin/harbor --version 2>/dev/null | tail -1)" || fail "$VENV missing (not visible from this node? the checkout must be on a shared filesystem)"
[[ -d LHTB/tasks-2.0 ]] && pass "LHTB tasks: $(ls -d LHTB/tasks-2.0/*/ | wc -l) in LHTB/tasks-2.0" || fail "LHTB/tasks-2.0 missing"
[[ -f .env ]] && grep -q '^OPENAI_API_KEY=.' .env && pass ".env has OPENAI_API_KEY" || fail ".env missing or without OPENAI_API_KEY"
if [[ $ENV_TYPE == singularity ]]; then
  if command -v singularity >/dev/null; then
    pass "singularity: $(singularity --version 2>&1 | head -1)"
    CHECK=${SIF_CACHE:-./data/sif-cache}/check; mkdir -p "$CHECK"
    export APPTAINER_CACHEDIR=${APPTAINER_CACHEDIR:-$PWD/data/sif-cache/oci} SINGULARITY_CACHEDIR=${SINGULARITY_CACHEDIR:-$PWD/data/sif-cache/oci}
    img=$(realpath -m "$CHECK")/python_3.11-slim.sif
    [[ -f $img ]] || singularity -q pull "$img" docker://python:3.11-slim >/dev/null 2>&1 || fail "cannot pull docker://python:3.11-slim (Docker Hub reachable from this node? PULL=1 on the login node avoids pulls in jobs)"
    if [[ -f $img ]]; then
      out=$(cd /tmp && singularity exec --no-mount home,tmp --fakeroot --writable-tmpfs --containall --pid "$img" \
            sh -c 'id -u; dd if=/dev/zero of=/root/x bs=1M count=768 2>/dev/null && echo W_OK; python3 -c "import socket; socket.getaddrinfo(\"registry.npmjs.org\", 443)" 2>/dev/null && echo DNS_OK' 2>&1)
      [[ $(echo "$out" | head -1) == 0 ]] && pass "fakeroot container runs as uid 0" || fail "fakeroot container failed: $(echo "$out" | tail -2 | tr '\n' ' ') (admin: /etc/subuid entry for $(id -un), or unprivileged user namespaces)"
      grep -q W_OK <<<"$out" && pass "writable layer takes 768 MB (Codex install ~0.5 GB; it lives in RAM)" \
        || fail "writable-tmpfs too small (admin: raise 'sessiondir max size' in apptainer.conf)"
      grep -q DNS_OK <<<"$out" && pass "DNS inside the container" || fail "no DNS inside the container"
    fi
  else fail "singularity/apptainer not on PATH (set PRE_CMD=\"module load apptainer\" in $CONF?)"; fi
else
  docker info >/dev/null 2>&1 && pass "docker usable by $(id -un)" || fail "docker not usable on this node (docker group? daemon running?)"
  [[ $HARBOR != fork || -d data/lhtb_v2 ]] && pass "task directory for docker" || fail "data/lhtb_v2 missing"
fi
# network from this node: OpenAI (through the proxy that runs on the node), Codex install (nvm from GitHub, node, npm), apt, images
for u in https://api.openai.com/v1/models https://registry.npmjs.org/@openai/codex https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh https://nodejs.org/dist/index.json http://deb.debian.org/debian/dists/stable/Release https://registry-1.docker.io/v2/; do
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$u")
  [[ $code =~ ^(200|301|302|401)$ ]] && pass "reach $u ($code)" || fail "cannot reach $u (http $code)"
done
mem=$(awk '/MemAvailable/ {printf "%d", $2/1024/1024}' /proc/meminfo)
echo "INFO  node RAM available ${mem} GB, $(nproc) CPUs visible, free disk here $(df -BG --output=avail . | tail -1 | tr -dc 0-9) GB"
echo
(( fails == 0 )) && echo "OK: ready -> bash scripts/slurm/submit_lhtb_par.sh" || { echo "$fails check(s) failed"; exit 1; }
