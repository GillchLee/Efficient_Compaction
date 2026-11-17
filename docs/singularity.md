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
* Disk: `.sif` images 0.5-3 GB each; the 45-task `hard` subset needs roughly 60-100 GB.
* Docker Hub limits anonymous pulls (about 100 per 6 h): for more images,
  `singularity remote login --username <user> docker://docker.io` first.

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
patch -p1 < patches/singularity-run-scripts.patch            # until merged into scripts/ (see below)
bash setup.sh --profile harbor25                             # .venv-h25: Harbor 0.25.0 + optlimit[proxy]
bash scripts/check_server.sh
.venv-h25/bin/python scripts/prep_swebench.py --singularity --subset hard   # 45 tasks -> data/swebench-verified-sif
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

Other arms: `--limit native`, `--proxy-fixed 80k`, `--balance codex-luna-medium [--net-rent]`,
`--carry-files`. Several arms can run at once: each has its own proxy port; keep
(sum of --concurrent) x 8 GB below the free RAM.

Bring results back for analysis: `jobs/<job>/` and `jobs/logs/<job>*` (proxy logs).

## Pending merge on the main host

`patches/singularity-run-scripts.patch` adds the singularity branch to `scripts/run_job.sh`
(no Docker check; `.sif` cache env) and `scripts/run_harness_job.sh` (proxy on 127.0.0.1, no
egress-allowlist flags). It is kept as a patch because those scripts were in use by running jobs;
apply it with `patch -p1` once they finish.
