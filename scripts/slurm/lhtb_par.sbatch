#!/usr/bin/env bash
#SBATCH --job-name=lhtb_par
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --output=jobs/logs/slurm-%x-%j.out
# One arm of the LHTB 2.0 comparison (Codex + GPT-6 Luna behind the optlimit proxy): starts the proxy on
# this node, runs the arm's trials (CONC at a time) and stops the proxy. Submitted by
# scripts/slurm/submit_lhtb_par.sh, which sets partition / time / cpus / memory and exports ARM;
# also runs without Slurm:   ARM=fpt_par bash scripts/slurm/lhtb_par.sbatch
# Resubmitting the same arm resumes the job directory (finished trials are kept).
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/../..}"
CONF=${CONF:-scripts/slurm/lhtb_par.conf}
# shellcheck disable=SC1090
source "$CONF"
ARM=${ARM:?ARM is not set (fpt | fpt_par | ...; see $CONF)}
JOB=luna_lhtb2_cx_${ARM}${SUFFIX:-}
CONFIG=configs/sweep/$JOB.yaml
[[ -f $CONFIG ]] || { echo "missing $CONFIG: run scripts/slurm/submit_lhtb_par.sh (it generates the configs)" >&2; exit 1; }
[[ -n "${PRE_CMD:-}" ]] && eval "$PRE_CMD"
mkdir -p jobs/logs
if [[ ${ENV_TYPE:-singularity} == singularity ]]; then
  export OPTLIMIT_SIF_CACHE=${SIF_CACHE:-./data/sif-cache}
  # image conversions and container session dirs go to node-local scratch when Slurm provides one
  export APPTAINER_CACHEDIR=${APPTAINER_CACHEDIR:-$PWD/data/sif-cache/oci} SINGULARITY_CACHEDIR=${SINGULARITY_CACHEDIR:-$PWD/data/sif-cache/oci}
else
  [[ -n "${PROXY_HOST:-}" ]] && export OPTLIMIT_PROXY_HOST=$PROXY_HOST
fi
echo "$(date '+%F %T') $JOB on $(hostname) (slurm job ${SLURM_JOB_ID:-none}, env ${ENV_TYPE:-singularity}, cpus ${SLURM_CPUS_PER_TASK:-?}, mem ${SLURM_MEM_PER_NODE:-?} MB)"
# run_job.sh waits until the node has n_concurrent x task memory free; inside a Slurm allocation the
# node's MemAvailable says nothing about our share, so the check is skipped (set HB_MEM_CHECK=1 to keep it).
export HB_MEM_CHECK=${HB_MEM_CHECK:-0}
status=0
bash scripts/run_job.sh "$CONFIG" || status=$?
echo "$(date '+%F %T') $JOB finished (exit $status)"
exit "$status"
