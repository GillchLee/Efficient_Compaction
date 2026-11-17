#!/usr/bin/env bash
# Prepare KernelBench as a Harbor-0.25 benchmark (scripts/kernelbench/SOURCE.md).
#   bash scripts/prep_kernelbench.sh --build-only            # build optlimit/kernelbench-env:cu128
#   bash scripts/prep_kernelbench.sh --representative        # 35 curated tasks -> data/kernelbench/tasks
#   bash scripts/prep_kernelbench.sh --level 1 --limit 5     # any adapter.py option passes through
#   bash scripts/prep_kernelbench.sh                         # all 250 tasks (levels 1-3)
# Env: KB_IMAGE (default optlimit/kernelbench-env:cu128), KB_OUT (default data/kernelbench/tasks),
#      KB_REBUILD=1 forces a rebuild.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=${KB_IMAGE:-optlimit/kernelbench-env:cu128}
OUT=${KB_OUT:-data/kernelbench/tasks}
BUILD_ONLY=0
ARGS=()
for x in "$@"; do
  case "$x" in
    --build-only) BUILD_ONLY=1 ;;
    *) ARGS+=("$x") ;;
  esac
done

if [[ "${KB_REBUILD:-0}" == 1 ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "$(date '+%F %T') building $IMAGE (nvidia/cuda 12.8.1 devel + torch cu128 + KernelBench)"
  DOCKER_BUILDKIT=1 docker build -t "$IMAGE" -f scripts/kernelbench/Dockerfile scripts/kernelbench
  echo "$(date '+%F %T') built $IMAGE"
else
  echo "image $IMAGE present"
fi
[[ $BUILD_ONLY == 1 ]] && exit 0

mkdir -p "$OUT"
# Generate inside the image (kernelbench package + problem files live there); write as the host user.
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD/scripts/kernelbench:/adapter:ro" -v "$PWD/$OUT:/out" \
  "$IMAGE" python /adapter/adapter.py --output-dir /out "${ARGS[@]}"
echo "$(ls -d "$OUT"/kernelbench-* 2>/dev/null | wc -l) task dirs in $OUT"
