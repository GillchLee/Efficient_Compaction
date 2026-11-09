#!/usr/bin/env python3
"""Copy Terminal-Bench 4.0 tasks into a job-ready directory for the LHTB-style trial loop.

    harbor datasets download terminal-bench/terminal-bench@4.0.0 -o data/tb4_src --export
    python scripts/prep_tb4.py --src data/tb4_src/terminal-bench --out data/tb4 TASK [TASK ...]

Each copied task.toml gets `continue_until_timeout = true` under [agent] so that the
bundled LHTB harbor keeps the agent working (interim verifier + continuation prompt)
until the agent budget (configs/make_config.py --hours) runs out or the verifier passes.
Nothing else in the task is changed. Without the flag a TB4 task is single-shot: the
first task_complete ends the trial, which is the official TB4 protocol.
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tasks", nargs="+")
    ap.add_argument("--src", default="data/tb4_src/terminal-bench")
    ap.add_argument("--out", default="data/tb4")
    ap.add_argument("--single-shot", action="store_true", help="copy without continue_until_timeout")
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in a.tasks:
        s = src / name
        if not (s / "task.toml").exists():
            raise SystemExit(f"no task.toml in {s}")
        d = out / name
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(s, d)
        if a.single_shot:
            continue
        toml = d / "task.toml"
        txt = toml.read_text()
        if re.search(r"^continue_until_timeout\s*=", txt, re.M):
            txt = re.sub(r"^continue_until_timeout\s*=.*$", "continue_until_timeout = true", txt, flags=re.M)
        elif re.search(r"^\[agent\]\s*$", txt, re.M):
            txt = re.sub(r"^\[agent\]\s*$", "[agent]\ncontinue_until_timeout = true", txt, count=1, flags=re.M)
        else:
            txt = txt.rstrip("\n") + "\n\n[agent]\ncontinue_until_timeout = true\n"
        toml.write_text(txt)
        print(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
