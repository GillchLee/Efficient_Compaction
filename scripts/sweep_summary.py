#!/usr/bin/env python3
"""Reward / token table across sweep jobs (one job per compaction window).

    python scripts/sweep_summary.py jobs/lhtb-luna-ctx* [--out jobs/sweep_luna_ctx.csv]

Reads <job>/token_turns/summary.csv (written by scripts/token_turns.py) and prints
reward per task for every job, plus mean reward, solves (R >= 0.95) and tokens.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SOLVED = 0.95


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_dirs", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    rows = []
    for job_dir in args.job_dirs:
        summary = job_dir / "token_turns" / "summary.csv"
        if not summary.exists():
            print(f"skip {job_dir}: no {summary} (run token_turns.py first)", file=sys.stderr)
            continue
        with summary.open() as f:
            for r in csv.DictReader(f):
                rows.append({"job": job_dir.name, **r})

    if not rows:
        print("no data", file=sys.stderr)
        return 1

    jobs = sorted({r["job"] for r in rows}, key=[d.name for d in args.job_dirs].index)
    tasks = sorted({r["task"] for r in rows})
    reward = {(r["job"], r["task"]): _float(r["reward"]) for r in rows}

    fmt = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
    width = max(len(t) for t in tasks)
    print(f"{'task':<{width}}  " + "  ".join(f"{j.rsplit('ctx', 1)[-1][-10:]:>10}" for j in jobs))
    for t in tasks:
        print(f"{t:<{width}}  " + "  ".join(f"{fmt(reward.get((j, t))):>10}" for j in jobs))

    out_rows = []
    for j in jobs:
        job_rows = [r for r in rows if r["job"] == j]
        rewards = [reward.get((j, t)) for t in tasks]
        scored = [v for v in rewards if v is not None]
        out_rows.append(
            {
                "job": j,
                "tasks": len(tasks),
                "scored": len(scored),
                # Unscored tasks count as 0, as in the LHTB leaderboard.
                "mean_reward": round(sum(scored) / len(tasks), 4),
                "solved": sum(v >= SOLVED for v in scored),
                "compactions": sum(int(r.get("compactions") or 0) for r in job_rows),
                "turns": sum(int(r["turns"]) for r in job_rows),
                "sum_prompt_tokens": sum(int(r["sum_prompt_tokens"]) for r in job_rows),
                "sum_cache_read": sum(int(r["sum_cache_read"]) for r in job_rows),
                "sum_cache_write": sum(int(r["sum_cache_write"]) for r in job_rows),
                "sum_completion_tokens": sum(int(r["sum_completion_tokens"]) for r in job_rows),
                "summ_calls": sum(int(r.get("summ_calls") or 0) for r in job_rows),
                "summ_prompt_tokens": sum(int(r.get("summ_prompt_tokens") or 0) for r in job_rows),
                "summ_completion_tokens": sum(
                    int(r.get("summ_completion_tokens") or 0) for r in job_rows
                ),
                "summ_cost_usd": round(sum(float(r.get("summ_cost_usd") or 0) for r in job_rows), 4),
                "cost_usd": round(sum(float(r["cost_usd"]) for r in job_rows), 4),
            }
        )
    print()
    for r in out_rows:
        print(
            f"{r['job']:<24} mean={r['mean_reward']:.3f} solved={r['solved']}/{r['tasks']} "
            f"compactions={r['compactions']} turns={r['turns']} "
            f"prompt={r['sum_prompt_tokens']:,} cache_read={r['sum_cache_read']:,} "
            f"cache_write={r['sum_cache_write']:,} output={r['sum_completion_tokens']:,} "
            f"${r['cost_usd']} (summ {r['summ_calls']} calls ${r['summ_cost_usd']})"
        )

    if args.out:
        with args.out.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(out_rows[0]))
            writer.writeheader()
            writer.writerows(out_rows)
        print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
