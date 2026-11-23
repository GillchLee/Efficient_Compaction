#!/usr/bin/env bash
# Generate the configs of the LHTB 2.0 arms and submit one Slurm job per arm.
#
#   bash scripts/slurm/submit_lhtb_par.sh            # generate configs + sbatch every arm in ARMS
#   DRY=1 bash scripts/slurm/submit_lhtb_par.sh      # only generate the configs and print the sbatch commands
#   ARMS="fpt_par" bash scripts/slurm/submit_lhtb_par.sh   # override any setting of the conf file from the environment
#
# Settings: scripts/slurm/lhtb_par.conf (copy of lhtb_par.conf.example). Server preparation (once):
# scripts/slurm/setup_server.sh. Each job writes jobs/<job>/ (trials), jobs/logs/<job>.proxy.jsonl
# (every model call) and jobs/logs/slurm-lhtb_<arm>-<id>.out.
set -euo pipefail
cd "$(dirname "$0")/../.."
CONF=${CONF:-scripts/slurm/lhtb_par.conf}
[[ -f $CONF ]] || { echo "missing $CONF: cp scripts/slurm/lhtb_par.conf.example $CONF and edit it" >&2; exit 1; }
# values already in the environment win over the conf file
OVERRIDES=$(for v in ENV_TYPE ARMS TASKS HOURS ATTEMPTS CONC EFFORT AGENT_VERSION PREMIUM SUFFIX PARTITION ACCOUNT QOS TIME CPUS_PER_TRIAL MEM_PER_TRIAL_GB EXTRA_SBATCH SIF_CACHE; do
  if [[ -n "${!v+x}" ]]; then printf '%s=%q\n' "$v" "${!v}"; fi; done)
# shellcheck disable=SC1090
source "$CONF"
eval "$OVERRIDES"
ENV_TYPE=${ENV_TYPE:-singularity}; HOURS=${HOURS:-3}; ATTEMPTS=${ATTEMPTS:-1}; CONC=${CONC:-2}
PY=.venv/bin/python
[[ -x $PY ]] || { echo "missing .venv: bash scripts/slurm/setup_server.sh" >&2; exit 1; }

if [[ $ENV_TYPE == docker ]]; then
  TASKS_DIR=./data/lhtb_v2          # network-enabled copies (setup_server.sh): the container must reach the proxy
  [[ -d $TASKS_DIR ]] || { echo "missing $TASKS_DIR: bash scripts/slurm/setup_server.sh" >&2; exit 1; }
else
  TASKS_DIR=./LHTB/tasks-2.0        # singularity containers share the node's network
fi
if [[ ${TASKS:-all} == all ]]; then
  # the 30 reconstruction tasks (pre-built image, continue_until_timeout); genetic-convergence-testing is left out
  TASKS=$(cd "$TASKS_DIR" && for d in */; do [[ -f $d/reconstruction.json ]] && echo "${d%/}"; done | tr '\n' ' ')
fi
read -r -a TASK_ARR <<<"$TASKS"
NT=${#TASK_ARR[@]}
(( NT > 0 )) || { echo "no tasks" >&2; exit 1; }
for t in "${TASK_ARR[@]}"; do [[ -d $TASKS_DIR/$t ]] || { echo "unknown task: $t (not in $TASKS_DIR)" >&2; exit 1; }; done

COMMON=(--harness codex --model openai/gpt-6-luna --tasks-dir "$TASKS_DIR" --tasks "${TASK_ARR[@]}"
        --reasoning-effort "${EFFORT:-medium}" --hours "$HOURS" --env "$ENV_TYPE"
        --concurrent "$CONC" --attempts "$ATTEMPTS")
[[ -n "${AGENT_VERSION:-}" ]] && COMMON+=(--agent-version "$AGENT_VERSION")
[[ $ENV_TYPE == singularity ]] && COMMON+=(--sif-cache "${SIF_CACHE:-./data/sif-cache}")
FORMULA=(--formula codex-luna-medium --prices gpt-6-luna --learn)
PT=(--productive-turn); [[ ${PREMIUM:-0} == 1 ]] && PT+=(--reread-premium)
arm_flags() {
  case "$1" in
    fpt)         echo "${FORMULA[*]} ${PT[*]}" ;;
    fpt_par)     echo "${FORMULA[*]} ${PT[*]} --reorient parallel" ;;
    fpt_plain)   echo "${FORMULA[*]} ${PT[*]} --reorient plain" ;;
    formula)     echo "${FORMULA[*]}" ;;
    formula_par) echo "${FORMULA[*]} --reorient parallel" ;;
    l80k)        echo "--proxy-fixed 80k" ;;
    l80k_par)    echo "--proxy-fixed 80k --reorient parallel" ;;
    l80k_plain)  echo "--proxy-fixed 80k --reorient plain" ;;
    native)      echo "--limit native --observe" ;;
    *) echo "unknown arm: $1" >&2; return 1 ;;
  esac
}

# resources of one arm job: CONC trials at a time, plus the proxy and Harbor itself
TRIALS=$(( NT * ATTEMPTS )); (( CONC > TRIALS )) && CONC_EFF=$TRIALS || CONC_EFF=$CONC
CPUS=$(( CONC_EFF * ${CPUS_PER_TRIAL:-2} + 2 ))
MEM_GB=$(( CONC_EFF * ${MEM_PER_TRIAL_GB:-8} + 4 ))
if [[ -z "${TIME:-}" ]]; then
  # per wave: the agent budget + interim/final verification and setup (about half as much again) + 30 min
  WAVES=$(( (TRIALS + CONC_EFF - 1) / CONC_EFF ))
  MIN=$($PY -c "import math,sys; print(math.ceil($WAVES * ($HOURS * 1.5 * 60 + 30)))")
  TIME=$(printf '%d-%02d:%02d:00' $(( MIN / 1440 )) $(( MIN % 1440 / 60 )) $(( MIN % 60 )))
fi
mkdir -p jobs/logs configs/sweep
echo "env $ENV_TYPE | $NT task(s) x $ATTEMPTS attempt(s) = $TRIALS trial(s) per arm, $CONC_EFF at a time, ${HOURS} h each"
echo "per arm job: --cpus-per-task $CPUS --mem ${MEM_GB}G --time $TIME"
for ARM in $ARMS; do
  FLAGS=$(arm_flags "$ARM")
  JOB=luna_lhtb2_cx_${ARM}${SUFFIX:-}
  # shellcheck disable=SC2086
  $PY configs/make_config.py "${COMMON[@]}" $FLAGS --job-name "$JOB" --out "configs/sweep/$JOB.yaml" >/dev/null
  SB=(sbatch --job-name "lhtb_$ARM${SUFFIX:-}" --cpus-per-task "$CPUS" --mem "${MEM_GB}G" --time "$TIME"
      --export "ALL,ARM=$ARM,CONF=$CONF")
  [[ -n "${PARTITION:-}" ]] && SB+=(--partition "$PARTITION")
  [[ -n "${ACCOUNT:-}" ]] && SB+=(--account "$ACCOUNT")
  [[ -n "${QOS:-}" ]] && SB+=(--qos "$QOS")
  # shellcheck disable=SC2206
  [[ -n "${EXTRA_SBATCH:-}" ]] && SB+=($EXTRA_SBATCH)
  SB+=(scripts/slurm/lhtb_par.sbatch)
  if [[ -n "${DRY:-}" ]]; then
    echo "[dry] configs/sweep/$JOB.yaml  <- $FLAGS"
    echo "[dry] ${SB[*]}"
  else
    echo "configs/sweep/$JOB.yaml: $("${SB[@]}")"
  fi
done
