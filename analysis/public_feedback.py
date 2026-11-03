#!/usr/bin/env python3
"""Agent-visible public feedback score per turn (LHTB 2.0 reconstruction tasks).

    python analysis/public_feedback.py jobs/<job>... [--out jobs/public_feedback.json]

LHTB 2.0 tasks ship /app/tools/feedback, which prints "public_pass_count": N. This reads
the newest terminal output of every episode prompt (agent/episode-*/prompt.txt) and
writes {"<job>/<trial>": [[turn, public_pass_count], ...]}; analysis/chart.py shows the
turn at which the public checks first reached their maximum. The public score is not a
proxy for the hidden verifier reward (it saturates within the first ~20 turns).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PAT = re.compile(r'"public_pass_count":\s*(\d+)')


def scan(job_dirs: list[Path]) -> dict:
    out = {}
    for job in job_dirs:
        for trial in sorted(job.glob("*__*")):
            rows = []
            for p in (trial / "agent").glob("episode-*/prompt.txt"):
                ep = int(p.parent.name.split("-")[1])
                txt = p.read_text(errors="ignore")
                # only the newest terminal output block (summarization hand-offs quote older ones)
                tail = txt.rsplit("New Terminal Output:", 1)[-1]
                m = PAT.findall(tail)
                if m:
                    rows.append([ep + 1, int(m[-1])])
            if rows:
                rows.sort()
                out[f"{job.name}/{trial.name}"] = rows
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job_dirs", type=Path, nargs="+")
    ap.add_argument("--out", type=Path, default=Path("jobs/public_feedback.json"))
    a = ap.parse_args()
    out = scan(a.job_dirs)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out))
    for k, v in out.items():
        print(f"{k[:70]:70s} n={len(v):3d} first={v[0]} last={v[-1]} max={max(r[1] for r in v)}")
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
