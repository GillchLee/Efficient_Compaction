#!/usr/bin/env bash
# Preflight for running SWE-bench Verified (or any Harbor 0.25 task with a docker_image) on a host
# WITHOUT Docker, through Singularity/Apptainer (docs/singularity.md). Read-only apart from a
# small test image in $CHECK_DIR (default ./data/sif-cache/check). Prints PASS / WARN / FAIL lines.
#
#   bash scripts/check_server.sh
set -uo pipefail
cd "$(dirname "$0")/.."
CHECK_DIR=$(realpath -m "${CHECK_DIR:-./data/sif-cache/check}")
fails=0
pass() { echo "PASS  $*"; }
warn() { echo "WARN  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }

# 1. tools
if command -v singularity >/dev/null; then pass "singularity: $(singularity --version 2>&1 | head -1)"
else fail "singularity/apptainer not on PATH (Apptainer >= 1.1 recommended; apptainer installs a 'singularity' alias)"; fi
command -v uv >/dev/null && pass "uv: $(uv --version)" || fail "uv missing (curl -LsSf https://astral.sh/uv/install.sh | sh)"
command -v git >/dev/null && pass "git" || fail "git missing"
command -v curl >/dev/null && pass "curl" || fail "curl missing"

# 2. user namespaces / fakeroot (Harbor runs every container with --fakeroot)
if [[ -r /proc/sys/kernel/apparmor_restrict_unprivileged_userns && $(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns) == 1 ]]; then
  warn "AppArmor restricts unprivileged user namespaces (Ubuntu >= 23.10); apptainer needs its AppArmor profile (installed by the apptainer .deb/PPA package) or fakeroot will fail"
fi
grep -q "^$(id -un):" /etc/subuid 2>/dev/null && pass "/etc/subuid has $(id -un)" \
  || warn "/etc/subuid has no entry for $(id -un): apptainer falls back to the fakeroot command (root-mapped namespace), usually fine for these images"

# 3. a real container: fakeroot, writable layer, DNS (Harbor's default no-mount drops resolv.conf; configs use home,tmp)
if command -v singularity >/dev/null; then
  mkdir -p "$CHECK_DIR"
  export APPTAINER_CACHEDIR=${APPTAINER_CACHEDIR:-$PWD/data/sif-cache/oci} SINGULARITY_CACHEDIR=${SINGULARITY_CACHEDIR:-$PWD/data/sif-cache/oci}
  img=$CHECK_DIR/alpine_3.20.sif
  [[ -f $img ]] || singularity -q pull "$img" docker://alpine:3.20 >/dev/null 2>&1 || fail "cannot pull docker://alpine:3.20 (Docker Hub reachable? APPTAINER_CACHEDIR writable?)"
  if [[ -f $img ]]; then
    out=$(cd /tmp && singularity exec --no-mount home,tmp --fakeroot --writable-tmpfs --containall --pid "$img" \
          sh -c 'id -u; dd if=/dev/zero of=/root/x bs=1M count=512 2>/dev/null && echo W_OK; nslookup pypi.org >/dev/null 2>&1 && echo DNS_OK' 2>&1)
    [[ $(echo "$out" | head -1) == 0 ]] && pass "fakeroot container runs as uid 0" || fail "fakeroot container failed: $(echo "$out" | tail -2 | tr '\n' ' ')"
    grep -q W_OK <<<"$out" && pass "writable layer takes 512 MB (it lives in RAM: budget it per trial)" \
      || fail "writable-tmpfs too small for 512 MB (admin: raise 'sessiondir max size' in apptainer.conf, or use fuse-overlayfs)"
    grep -q DNS_OK <<<"$out" && pass "DNS inside the container" || fail "no DNS inside the container (check 'config resolv_conf = yes' in apptainer.conf)"
  fi
fi

# 4. network: PyPI (grader + harbor server bootstrap), Docker Hub (images), OpenAI (agent via the local proxy)
for u in https://pypi.org/simple/pip/ https://registry-1.docker.io/v2/ https://api.openai.com/v1/models https://astral.sh/uv/0.7.13/install.sh; do
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$u")
  [[ $code =~ ^(200|301|302|401)$ ]] && pass "reach $u ($code)" || fail "cannot reach $u (http $code)"
done

# 5. resources: per trial ~4 GB task + up to ~2-4 GB writable layer + ~10 GB disk per image
mem=$(awk '/MemAvailable/ {printf "%d", $2/1024/1024}' /proc/meminfo)
disk=$(df -BG --output=avail . | tail -1 | tr -dc 0-9)
echo "INFO  available RAM ${mem} GB -> suggested concurrency <= $(( mem / 8 )) (8 GB per SWE-bench trial)"
echo "INFO  free disk ${disk} GB here; .sif images take ~0.5-3 GB each (45-task 'hard' subset ~60-100 GB)"
[[ -f .env ]] && grep -q '^OPENAI_API_KEY=.' .env && pass ".env has OPENAI_API_KEY" || warn ".env missing or without OPENAI_API_KEY (copy it over yourself)"
[[ -x .venv-h25/bin/harbor ]] && pass "harbor: $(.venv-h25/bin/harbor --version 2>/dev/null | tail -1)" || warn ".venv-h25 missing: bash setup.sh --profile harbor25"
echo
(( fails == 0 )) && echo "OK: ready" || { echo "$fails check(s) failed"; exit 1; }
