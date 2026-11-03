#!/usr/bin/env python3
"""Information loss of a compaction measured without a verifier: command re-acquisition.

    python analysis/reacquisition.py JOB_DIR... [--k 10] [--out out.csv]

After a hand-off the agent often re-runs commands it already ran before the compaction
(`cat` the same file, `ls`, re-check a tool version, re-run the public feedback script):
it is re-acquiring state the summary dropped.  For every agent turn we record whether its
commands (normalised keystrokes) already occurred earlier in the trial, and the turn's
distance from the last compaction.  The excess repeat rate in the first `k` turns after a
compaction, over the repeat rate deep inside windows, is the re-acquisition cost of that
hand-off, in turns per compaction.  Both are observable in any harness and any task.

Columns: job, trial, task, condition, compactions, turns, Lp_med (post-compaction write),
rate_after (repeat rate turns 1..k after a compaction), rate_base (turns > k), excess_turns
(sum over 1..k of rate - base), reacq_turns_per_compaction, share_readonly (fraction of the
repeated commands that are pure reads: cat/ls/head/grep/...).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics as st
import sys
from pathlib import Path

READ_ONLY = re.compile(r"^(cat|ls|head|tail|sed -n|grep|rg|find|wc|pwd|which|command -v|type|stat|file|du|df|echo|python3? -c|git (status|log|diff)|nl|less|more|tree|jq)\b")


def norm(cmd: str) -> str:
    c = re.sub(r"\s+", " ", cmd.strip())
    c = re.sub(r"\b\d{4,}\b", "N", c)          # big numbers / ids
    return c[:120]


def trial_rows(job: Path, trial: Path, k: int) -> dict | None:
    tj = trial / "agent" / "trajectory.json"
    if not tj.exists():
        return None
    steps = json.loads(tj.read_text())["steps"]
    seen: set[str] = set()
    since = None            # turns since last compaction (None before the first)
    comps = 0
    per_pos: dict[int, list[int]] = {}   # pos -> [repeat flags]
    base: list[int] = []
    ro_rep = rep = 0
    turns = 0
    post_write: list[int] = []
    for s in steps:
        if s.get("source") == "system" and "summarization" in (s.get("message") or ""):
            comps += 1
            since = 0
            continue
        if s.get("source") != "agent":
            continue
        turns += 1
        cmds = [norm(tc["arguments"].get("keystrokes", "")) for tc in (s.get("tool_calls") or [])
                if isinstance(tc, dict) and isinstance(tc.get("arguments"), dict)]
        cmds = [c for c in cmds if c and len(c) > 3]
        if since is not None:
            since += 1
            if since == 1:
                m = s.get("metrics") or {}
                cw = (m.get("extra") or {}).get("cache_creation_tokens")
                if cw is None and m.get("prompt_tokens") is not None:
                    cw = m["prompt_tokens"] - (m.get("cached_tokens") or 0)
                if cw:
                    post_write.append(int(cw))
        if cmds:
            flags = [c in seen for c in cmds]
            r = int(any(flags))
            for c, f in zip(cmds, flags):
                if f:
                    rep += 1
                    if READ_ONLY.match(c):
                        ro_rep += 1
            if since is not None and since <= k:
                per_pos.setdefault(since, []).append(r)
            elif since is None or since > k:
                base.append(r)
        seen.update(cmds)
    if turns < 5:
        return None
    rb = st.mean(base) if base else 0.0
    rates = {p: st.mean(v) for p, v in per_pos.items()}
    after = [x for v in per_pos.values() for x in v]
    excess = sum(max(0.0, rates[p] - rb) for p in rates)
    cond = ""
    summ = job / "token_turns" / "summary.csv"
    if summ.exists():
        for r in csv.DictReader(summ.open()):
            if r["trial"] == trial.name:
                cond = r["condition"]
    return {"job": job.name, "trial": trial.name, "task": trial.name.split("__")[0][:28], "condition": cond,
            "compactions": comps, "turns": turns, "Lp_med": int(st.median(post_write)) if post_write else "",
            "rate_after": round(st.mean(after), 3) if after else "", "rate_base": round(rb, 3),
            "excess_turns": round(excess, 2), "reacq_turns_per_compaction": round(excess, 2) if comps else "",
            "share_readonly": round(ro_rep / rep, 2) if rep else "",
            "rates_by_pos": " ".join(f"{p}:{rates[p]:.2f}" for p in sorted(rates))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job_dirs", type=Path, nargs="+")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    rows = []
    for j in a.job_dirs:
        for t in sorted(p for p in j.iterdir() if p.is_dir() and "__" in p.name):
            r = trial_rows(j, t, a.k)
            if r:
                rows.append(r)
    if not rows:
        print("no trajectories", file=sys.stderr)
        return 1
    print(f"{'job':<28} {'task':<22} {'cond':<10} {'comp':>4} {'turns':>5} {'Lp':>6} {'after':>6} {'base':>6} {'excess':>6} {'ro':>4}  rate by turns-since-compaction")
    for r in rows:
        print(f"{r['job'][:28]:<28} {r['task'][:22]:<22} {r['condition'][:10]:<10} {r['compactions']:>4} {r['turns']:>5} "
              f"{str(r['Lp_med']):>6} {str(r['rate_after']):>6} {r['rate_base']:>6} {r['excess_turns']:>6} {str(r['share_readonly']):>4}  {r['rates_by_pos']}")
    if a.out:
        with a.out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print(f"-> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
