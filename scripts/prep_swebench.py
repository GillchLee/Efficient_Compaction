#!/usr/bin/env python3
"""SWE-bench Verified (Harbor adapter, 500 tasks) as a local task directory, for Docker or Singularity.

    python scripts/prep_swebench.py                                  # docker:      data/swebench-verified
    python scripts/prep_swebench.py --singularity                    # singularity: data/swebench-verified-sif
    python scripts/prep_swebench.py --singularity --subset hard      # only the 45 tasks labelled 1-4 h / >4 h
    python scripts/prep_swebench.py --singularity --subset hard --pull   # + convert their images to .sif now

The tasks come from laude-institute/harbor-datasets at the commit the Harbor registry pins
(sparse checkout into data/_src/harbor_datasets, ~30 MB).  Every task image is a public
swebench/sweb.eval.x86_64.* image plus two Dockerfile lines (uv for the grader, /logs).

Singularity/Apptainer variant (hosts without Docker): Harbor's singularity environment runs
`docker_image` from task.toml and ignores the Dockerfile, so the variant
  * sets [environment] docker_image to the Dockerfile's FROM image, and
  * makes tests/test.sh install uv 0.7.13 itself when the image has none (the grader runs
    `uv run parser.py`, which also fetches swebench from PyPI: the verifier needs internet).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/laude-institute/harbor-datasets.git"
COMMIT = "86723674f04e4209ac479d0fb75d9d9f44b4377e"     # harbor registry: swebench-verified 1.0
SUB = "datasets/swebench-verified"
UV_LINE = 'export PATH="/root/.local/bin:$PATH"'
UV_BOOT = ('# singularity variant (scripts/prep_swebench.py): the image has no uv when the Dockerfile is not built\n'
           'if ! command -v uv >/dev/null 2>&1 && [ ! -x /root/.local/bin/uv ]; then\n'
           '  curl -LsSf https://astral.sh/uv/0.7.13/install.sh | env UV_INSTALL_DIR=/root/.local/bin UV_NO_MODIFY_PATH=1 sh\n'
           'fi\n'
           'mkdir -p /logs/verifier\n')
SUBSETS = {"all": None, "hard": ('"1-4 hours"', '">4 hours"'),
           "medium+": ('"15 min - 1 hour"', '"1-4 hours"', '">4 hours"')}


def fetch(src: Path) -> Path:
    tasks = src / SUB
    if tasks.is_dir():
        return tasks
    src.parent.mkdir(parents=True, exist_ok=True)
    run = lambda *a, cwd=None: subprocess.run(a, cwd=cwd, check=True)
    run("git", "clone", "-q", "--filter=blob:none", "--no-checkout", REPO, str(src))
    run("git", "sparse-checkout", "set", "--no-cone", SUB, cwd=src)
    run("git", "checkout", "-q", COMMIT, cwd=src)
    return tasks


def convert(task: Path) -> None:
    df = (task / "environment" / "Dockerfile").read_text()
    m = re.search(r"^FROM\s+(\S+)", df, re.M)
    if not m:
        raise SystemExit(f"{task.name}: no FROM line")
    toml = task / "task.toml"
    t = toml.read_text()
    if "docker_image" not in t:
        t = t.replace("[environment]\n", f'[environment]\ndocker_image = "{m.group(1)}"\n', 1)
        toml.write_text(t)
    sh = task / "tests" / "test.sh"
    s = sh.read_text()
    if "singularity variant" not in s:
        if UV_LINE not in s:
            raise SystemExit(f"{task.name}: test.sh has no '{UV_LINE}' line")
        sh.write_text(s.replace(UV_LINE, UV_BOOT + UV_LINE, 1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--singularity", action="store_true", help="write the Singularity/Apptainer variant")
    ap.add_argument("--subset", choices=sorted(SUBSETS), default="all",
                    help="hard = the 45 tasks labelled '1-4 hours' or '>4 hours' (longest trajectories)")
    ap.add_argument("--tasks", nargs="*", help="explicit task names (instead of --subset)")
    ap.add_argument("--src", type=Path, default=ROOT / "data/_src/harbor_datasets")
    ap.add_argument("--out", type=Path, help="default data/swebench-verified[-sif]")
    ap.add_argument("--pull", action="store_true",
                    help="singularity: convert the tasks' images to .sif now, one at a time, under the names Harbor "
                         "looks for (skips existing ones); runs then start without pulls and arms never race")
    ap.add_argument("--sif-cache", type=Path, default=ROOT / "data/sif-cache",
                    help="where --pull puts the .sif files (same as make_config --sif-cache; default data/sif-cache)")
    ap.add_argument("--keep-oci", action="store_true", help="--pull: keep the OCI layer cache (default: delete it, ~half the disk)")
    a = ap.parse_args()
    tasks_dir = fetch(a.src)
    out = a.out or ROOT / ("data/swebench-verified-sif" if a.singularity else "data/swebench-verified")
    names = sorted(p.name for p in tasks_dir.iterdir() if (p / "task.toml").exists())
    if a.tasks:
        missing = set(a.tasks) - set(names)
        if missing:
            raise SystemExit(f"unknown tasks: {sorted(missing)}")
        names = list(a.tasks)
    elif SUBSETS[a.subset]:
        names = [n for n in names if any(f"difficulty = {d}" in (tasks_dir / n / "task.toml").read_text()
                                         for d in SUBSETS[a.subset])]
    out.mkdir(parents=True, exist_ok=True)
    for n in names:
        dst = out / n
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(tasks_dir / n, dst)
        if a.singularity:
            convert(dst)
    print(f"{len(names)} tasks -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}"
          f"{' (singularity variant)' if a.singularity else ''}")
    if a.pull:
        return pull(out, names, a.sif_cache, a.keep_oci)
    return 0


def pull(out: Path, names: list[str], cache: Path, keep_oci: bool) -> int:
    """Pre-convert images under Harbor's cache names (harbor/environments/singularity: <image with / and : -> _>.sif)."""
    import os
    import time
    cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    oci = Path(env.get("APPTAINER_CACHEDIR") or env.get("SINGULARITY_CACHEDIR") or cache / "oci")
    env.setdefault("APPTAINER_CACHEDIR", str(oci))
    env.setdefault("SINGULARITY_CACHEDIR", str(oci))
    images = []
    for n in names:
        m = re.search(r'^docker_image\s*=\s*"([^"]+)"', (out / n / "task.toml").read_text(), re.M)
        if m and m.group(1) not in images:
            images.append(m.group(1))
    failed = []
    for i, img in enumerate(images, 1):
        full = img if ":" in img.rsplit("/", 1)[-1] else img + ":latest"
        sif = cache / (full.replace("/", "_").replace(":", "_") + ".sif")
        if sif.exists():
            print(f"[{i}/{len(images)}] cached {sif.name}")
            continue
        tmp = sif.with_suffix(".sif.tmp.prep")
        t0 = time.time()
        r = subprocess.run(["singularity", "-q", "pull", "--force", str(tmp), f"docker://{full}"], env=env,
                           capture_output=True, text=True)
        if r.returncode == 0 and tmp.exists():
            tmp.rename(sif)
            print(f"[{i}/{len(images)}] {full} {sif.stat().st_size / 1e9:.1f} GB {time.time() - t0:.0f} s")
        else:
            tmp.unlink(missing_ok=True)
            failed.append(full)
            print(f"[{i}/{len(images)}] FAILED {full}: {(r.stderr or r.stdout).strip()[-300:]}")
            if "toomanyrequests" in (r.stderr or "") or "rate limit" in (r.stderr or "").lower():
                print("Docker Hub rate limit: log in (apptainer registry login --username <user> docker://docker.io) "
                      "or wait, then rerun --pull (finished images are kept)")
                break
        if not keep_oci and oci.is_dir():
            shutil.rmtree(oci, ignore_errors=True)
    print(f"{len(images) - len(failed)}/{len(images)} images in {cache}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
