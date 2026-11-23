# Running on a host without Docker (Singularity / Apptainer)

For Codex / Claude Code (proxy harnesses) on the Harbor 0.25 profile, with tasks that name a
pre-built image (`[environment] docker_image`). Prepared for SWE-bench Verified; tested 2026-11-17
in an Ubuntu 24.04 + Apptainer 1.5.5 container without Docker (oracle 2 tasks, Codex + GPT-6 Luna
+ optlimit proxy 1 task).

## What differs from Docker

| | Docker | Singularity |
|---|---|---|
| image | built from `environment/Dockerfile` | `docker_image` pulled and converted to `.sif` (cached in `data/sif-cache`) |
| network | per-trial network, egress allowlists | the host's network: no isolation, no ~30-network cap, proxy on `127.0.0.1` |
| memory | `memory` from task.toml enforced by cgroups | not enforced: Harbor kills a container at 95 % of `memory`; the writable layer is in RAM |
| root | container root | `--fakeroot` (user namespace) |

Harbor's default `singularity_no_mount=home,tmp,bind-paths` also drops `/etc/resolv.conf`, so the
container has no DNS (Harbor's in-container server cannot install pip, the grader cannot reach PyPI).
`make_config.py --env singularity` writes `singularity_no_mount: home,tmp`.

## Server requirements

* Apptainer >= 1.1 (or SingularityCE) on PATH as `singularity`, and `--fakeroot` working for your
  user. On Ubuntu >= 23.10 unprivileged user namespaces are restricted by AppArmor; the apptainer
  package from the PPA/.deb installs the profile that allows them (an admin install).
* `uv`, `git`, `curl`; outbound HTTPS to Docker Hub, PyPI, astral.sh, api.openai.com.
* RAM: about 8 GB per concurrent SWE-bench trial (4 GB task + writable layer + agent).
* Disk: `.sif` images ~1 GB each (measured 1.0-1.1 GB); the 45-task `hard` subset ~45-60 GB,
  all 500 ~0.5 TB. `--pull` deletes the OCI layer cache after each image.
* Docker Hub pull limits (2025 policy, check the current one): anonymous about 10 per hour per IP,
  free account about 100 per hour. Log in before pulling more than a few images:
  `apptainer registry login --username <user> docker://docker.io` (token as password).

`bash scripts/check_server.sh` checks all of this (pulls alpine once, runs one container) and
prints PASS / WARN / FAIL.

## Setup (once per server)

```bash
# from this host: code only (no jobs/, data/, venvs); the .env with the API key you copy yourself
rsync -a --exclude=/jobs --exclude=/data --exclude=/LHTB --exclude='/.venv*' --exclude=/.tools \
      --exclude=__pycache__ ./ <server>:<dir>/Efficient_Compaction_new/
scp .env <server>:<dir>/Efficient_Compaction_new/.env        # never commit / paste it

# on the server
cd <dir>/Efficient_Compaction_new
bash setup.sh --profile harbor25                             # .venv-h25: Harbor 0.25.0 + optlimit[proxy]
bash scripts/check_server.sh
.venv-h25/bin/python scripts/prep_swebench.py --singularity --subset hard --pull
#   45 tasks -> data/swebench-verified-sif, and their 45 images -> data/sif-cache (~55 s / ~1 GB each);
#   rerun after a rate-limit stop: finished images are kept
```

`--subset hard` = the tasks labelled "1-4 hours" (42) or ">4 hours" (3), the longest trajectories;
`--subset all` (500) or `--tasks NAME ...` also work.

## Smoke test, then runs

```bash
# no API cost: the reference solution on two tasks
APPTAINER_CACHEDIR=$PWD/data/sif-cache/oci .venv-h25/bin/harbor run -p data/swebench-verified-sif \
  -a oracle -e singularity --ek singularity_image_cache_dir=$PWD/data/sif-cache \
  --ek singularity_no_mount=home,tmp -n 2 -i pallets__flask-5014 -i psf__requests-1921 -o jobs --job-name sif_oracle
#   psf__requests-1921 scores 0 with the oracle under Docker too (its tests call httpbin.org)

# one arm (Codex + GPT-6 Luna, optlimit formula)
.venv-h25/bin/python configs/make_config.py --harness codex --model openai/gpt-6-luna \
  --formula codex-luna-medium --prices gpt-6-luna --learn --reasoning-effort medium \
  --benchmark swebench_verified_sif --env singularity --concurrent 4 \
  --job-name swev_cx_formula --out configs/sweep/swev_cx_formula.yaml
setsid nohup bash scripts/run_job.sh configs/sweep/swev_cx_formula.yaml >jobs/logs/swev_cx_formula.chain.log 2>&1 &
```

### Hard subset, all arms, two attempts each

```bash
CONC=6 setsid nohup bash scripts/run_swev_hard.sh >/dev/null 2>&1 &
tail -f jobs/logs/swev_hard_cx.chain.log
```

Arms native / fixed 80K / formula / balance + Net rent run in parallel (`ARMS=...` to change;
also `l40k`, `balance`, `formula_carry`), each 45 tasks x `ATTEMPTS` (2), `CONC` trials at a time per
arm. RAM ~ arms x CONC x 8 GB (4 arms x 6 = 24 trials ~ 190 GB). Time ~ 45 x 2 x 25 min / CONC per arm
(CONC 6: about 6-7 h). Codex reasoning effort `EFFORT` (default medium, prior codex-luna-medium).

Other arms: `--limit native`, `--proxy-fixed 80k`, `--balance codex-luna-medium [--net-rent]`,
`--carry-files`. Several arms can run at once: each has its own proxy port; keep
(sum of --concurrent) x 8 GB below the free RAM.

Bring results back for analysis: `jobs/<job>/` and `jobs/logs/<job>*` (proxy logs).

## LHTB 2.0 on a Slurm cluster (`scripts/slurm/`)

LHTB tasks set `continue_until_timeout`: after the agent stops, the verifier runs, and on a rejection the
agent goes on in the same conversation until the budget ends. Two Harbor bases can run that
(`HARBOR` in `scripts/slurm/lhtb_par.conf`):

| `HARBOR` | venv | where the loop comes from | Singularity |
|---|---|---|---|
| `0.25` (default) | `.venv-h25` (stock Harbor 0.25.0) | `optlimit/adapters/harbor25_continue.py`, active when `HB_CONTINUE_MODE=same_conversation` (LHTB configs carry it) | built in (`--fakeroot`) |
| `fork` | `.venv` (LHTB's Harbor 0.7 + `patches/lhtb-7ca0cac`) | the fork's `trial.py` | needs patch 06 (`singularity_fakeroot`) |

Stock Harbor has no such loop: without the module an LHTB trial on 0.25 ends at the agent's first
"done". The module wraps `SingleStepTrial._run_agent` (no file of the package is replaced), uses the
agent's native `resume()` (Codex: `codex exec resume --last`) and the fork's rejection text, and takes
the grader isolation from LHTB's own stock-Harbor replacement file (`LHTB/harbor/patches/
single_step.py.harbor-0.20.0`): hidden tests are removed and the verifier's output is moved to
`<trial>/interim_verifier_artifacts/` before the agent goes on. Every interim verification is a line in
`<trial>/interim_verifier.jsonl`. Results from the two bases are not interchangeable (different
Harbor, and the fork leaves the grader's log readable on Docker): keep one base within a comparison.

LHTB 2.0 tasks name a pre-built `docker_image`, and Singularity containers share the node's network,
so the stock task directory works. With Docker, Harbor 0.25 does not read the tasks' `network = false`
(it logs "the effective network policy is public"), so the stock directory works there too; the fork
applies the key from container start and needs network-enabled copies (`data/lhtb_v2`, made by
`setup_server.sh`). Either way the agent container has network access in these runs, unlike LHTB's
own leaderboard protocol.

```bash
cp scripts/slurm/lhtb_par.conf.example scripts/slurm/lhtb_par.conf     # edit: ENV_TYPE, HARBOR, ARMS, TASKS, HOURS, CONC, Slurm fields
bash scripts/slurm/setup_server.sh                                     # login node: Harbor venv, LHTB tasks, checks
srun -p <partition> -c 2 --mem 8G -t 10 bash scripts/slurm/setup_server.sh --check-only   # the same checks on a compute node
PULL=1 bash scripts/slurm/setup_server.sh                              # optional: convert the task images on the login node
DRY=1 bash scripts/slurm/submit_lhtb_par.sh                            # configs + the sbatch commands, nothing submitted
bash scripts/slurm/submit_lhtb_par.sh                                  # one Slurm job per arm
```

Each arm job (`lhtb_par.sbatch`) starts its own proxy on the node, runs `CONC` trials at a time and
writes `jobs/luna_lhtb2_cx_<arm>/` and `jobs/logs/luna_lhtb2_cx_<arm>.proxy.jsonl`. Resubmitting an
arm resumes its job directory (trials without a result are rerun; a trial that ended with an error
keeps its result and is not).

Harbor 0.25's Singularity backend installs its command server inside every container at start (venv,
`get-pip.py`, `pip install uvicorn fastapi`) and waits 60 s for it; on a slow PyPI link starts failed
("Failed to start Singularity FastAPI server after 3 port attempts"). `setup.sh --profile harbor25`
raises the wait to 300 s. Compute nodes need PyPI for this, besides npm / GitHub / apt for Codex.

Checked 2026-11-23/24 without Slurm and without API spend (`scripts/fake_llm_upstream.py` as the
model, Apptainer 1.5.5 in a container): on both bases image conversion, fakeroot container, Codex
0.171.0 install, proxy on 127.0.0.1, interim verifier and same-conversation resume (61 resumes in one
Codex session on 0.25), compaction (20 remote compactions on 0.25) with the re-orientation note, final
verifier. Docker with Harbor 0.25 was checked the same way on the development host (41 resumes, 13
compactions in one session). Not checked: a real Slurm allocation, real-model behaviour over hours,
Docker with the fork through these scripts.
