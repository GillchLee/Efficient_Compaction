#!/usr/bin/env python3
"""Per-trial reward, partial score and timing from Harbor job directories.

    python analysis/partial_scores.py jobs/<job>... [--csv OUT]

SWE-Marathon verifiers write verifier/metrics.json with partial_score / pass_rate next to the
binary reward; FrontierSWE writes verifier/reward.json (reward is already continuous). One row
per trial: job, task, agent, agent_minutes, reward, partial, exception.
"""
from __future__ import annotations
import argparse, csv, datetime, json, sys
from pathlib import Path


def _ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def trial_row(d: Path) -> dict | None:
    res = d / "result.json"
    if not res.exists():
        return None
    r = json.loads(res.read_text())
    a = r.get("agent_execution") or {}
    mins = None
    if a.get("started_at") and a.get("finished_at"):
        mins = round((_ts(a["finished_at"]) - _ts(a["started_at"])).total_seconds() / 60, 1)
    rewards = (r.get("verifier_result") or {}).get("rewards") or {}
    partial = None
    m = d / "verifier" / "metrics.json"
    if m.exists():
        try:
            mm = json.loads(m.read_text())
            partial = mm.get("partial_score", mm.get("pass_rate"))
        except Exception:
            pass
    if partial is None and rewards:
        partial = rewards.get("score", rewards.get("reward"))
    exc = (r.get("exception_info") or {}).get("exception_type")
    return dict(job=d.parent.name, task=r.get("task_name", d.name), agent=(r.get("agent_info") or {}).get("name"),
                agent_minutes=mins, reward=rewards.get("reward"), partial=partial, exception=exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs", nargs="+")
    ap.add_argument("--csv")
    a = ap.parse_args()
    rows = []
    for j in a.jobs:
        for d in sorted(Path(j).glob("*__*")):
            row = trial_row(d)
            if row:
                rows.append(row)
    cols = ["job", "task", "agent", "agent_minutes", "reward", "partial", "exception"]
    out = open(a.csv, "w", newline="") if a.csv else sys.stdout
    w = csv.DictWriter(out, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)


if __name__ == "__main__":
    main()
