#!/usr/bin/env python3
"""Reward as a function of time, turns and spend: the "accuracy over time scale" view.

    python analysis/reward_curve.py jobs/luna10_l60k jobs/luna10_formula_nog \
        [--cond JOB=LABEL ...] [--grid 600] [--out jobs/reward_curve.csv] [--md jobs/reward_curve.md]

Inputs per trial: <trial>/interim_verifier.jsonl (one row per submission: the hidden verifier's
reward at that moment; patch 03), result.json (final reward, agent start/end),
<job>/token_turns/turns.csv (analysis/token_turns.py: timestamp, cost_usd, completion_tokens per
turn) to place each submission on the turn and spend axes.

Outputs
  points.csv  one row per submission: cond, job, task, trial, submission, t_sec, t_frac, turn,
              cum_cost_usd, cum_output_tokens, reward, passed  (+ a "final" row from result.json)
  curve.csv   reward carried forward on a time grid (--grid seconds) per trial and the mean per
              condition x task (reward before the first submission counts 0)
  summary     per condition x task: submissions, first submission (s), reward at 25/50/75/100 % of
              the budget, time to best, time to full pass (if any), area under the curve
              (mean reward over the budget = "time-weighted accuracy"), final reward.
The agent never sees these verifier results unless HB_VERIFIER_FEEDBACK says so (patch 04);
the sampling times are the agent's own submissions, so the curve is a step function.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import fmean


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_turns(job: Path) -> dict[str, list[dict]]:
    f = job / "token_turns" / "turns.csv"
    out: dict[str, list[dict]] = defaultdict(list)
    if not f.exists():
        return out
    with f.open() as fh:
        for r in csv.DictReader(fh):
            if r.get("kind", "main") != "main" or not r.get("timestamp"):
                continue
            out[r["trial"]].append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r["timestamp"])
    return out


def trial_points(job: Path, trial: Path, turns: list[dict]) -> tuple[list[dict], float] | None:
    res = trial / "result.json"
    if not res.exists():
        return None
    r = json.loads(res.read_text())
    ae = r.get("agent_execution") or {}
    if not ae.get("started_at"):
        return None
    t0 = ts(ae["started_at"])
    cfg = json.loads((job / "config.json").read_text())
    budget = float(cfg["agents"][0].get("override_timeout_sec") or 0) or (
        (ts(ae["finished_at"]) - t0).total_seconds() if ae.get("finished_at") else 0)
    # cumulative cost / output per turn timestamp
    cum_cost, cum_out, tt = [], [], []
    c = o = 0.0
    for row in turns:
        c += float(row.get("cost_usd") or 0); o += float(row.get("completion_tokens") or 0)
        cum_cost.append(c); cum_out.append(o); tt.append(ts(row["timestamp"]))

    def at(t: datetime) -> tuple[int, float, float]:
        n = sum(1 for x in tt if x <= t)
        return n, (cum_cost[n - 1] if n else 0.0), (cum_out[n - 1] if n else 0.0)

    pts = []
    iv = trial / "interim_verifier.jsonl"
    if iv.exists():
        for line in iv.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("reward") is None:
                continue
            t = ts(rec["timestamp"]); dt = (t - t0).total_seconds()
            n, cc, co = at(t)
            pts.append({"submission": rec["submission"], "t_sec": round(dt, 1),
                        "t_frac": round(dt / budget, 4) if budget else None, "turn": n,
                        "cum_cost_usd": round(cc, 5), "cum_output_tokens": int(co),
                        "reward": float(rec["reward"]), "passed": bool(rec.get("passed"))})
    final = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if final is not None:
        tend = ts(ae["finished_at"]) if ae.get("finished_at") else t0
        n, cc, co = at(tend)
        pts.append({"submission": "final", "t_sec": round(budget, 1), "t_frac": 1.0, "turn": n,
                    "cum_cost_usd": round(cc, 5), "cum_output_tokens": int(co),
                    "reward": float(final), "passed": float(final) >= 1.0})
    return pts, budget


def step_value(pts: list[dict], t: float) -> float:
    v = 0.0
    for p in pts:
        if p["t_sec"] <= t:
            v = p["reward"]
        else:
            break
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("jobs", nargs="+")
    ap.add_argument("--cond", action="append", default=[], metavar="JOB=LABEL")
    ap.add_argument("--grid", type=float, default=600.0, help="seconds between curve samples")
    ap.add_argument("--out", help="points csv (curve csv goes next to it as *_curve.csv)")
    ap.add_argument("--md", help="write the summary table as markdown")
    a = ap.parse_args()
    cond_of = dict(kv.split("=", 1) for kv in a.cond)

    points: list[dict] = []
    budgets: dict[str, float] = {}
    for j in a.jobs:
        job = Path(j)
        if not (job / "config.json").exists():
            print(f"skip {job}: no config.json", file=sys.stderr); continue
        turns = load_turns(job)
        cond = cond_of.get(job.name, job.name)
        for trial in sorted(p for p in job.iterdir() if p.is_dir() and "__" in p.name):
            got = trial_points(job, trial, turns.get(trial.name, []))
            if not got:
                continue
            pts, budget = got
            budgets[cond] = budget
            task = trial.name.rsplit("__", 1)[0]
            for p in pts:
                points.append({"cond": cond, "job": job.name, "task": task, "trial": trial.name, **p})

    # curves and summary
    by_trial: dict[tuple, list[dict]] = defaultdict(list)
    for p in points:
        by_trial[(p["cond"], p["task"], p["trial"])].append(p)
    for pts in by_trial.values():
        pts.sort(key=lambda p: p["t_sec"])
    curve_rows, summary = [], []
    for (cond, task, trial), pts in sorted(by_trial.items()):
        budget = budgets[cond]
        grid = [k * a.grid for k in range(int(budget // a.grid) + 1)] + [budget]
        vals = [step_value(pts, t) for t in grid]
        for t, v in zip(grid, vals):
            curve_rows.append({"cond": cond, "task": task, "trial": trial, "t_sec": t, "reward": v})
        subs = [p for p in pts if p["submission"] != "final"]
        auc = sum((grid[i + 1] - grid[i]) * vals[i] for i in range(len(grid) - 1)) / budget if budget else 0
        best = max((p["reward"] for p in pts), default=0.0)
        t_best = next((p["t_sec"] for p in pts if p["reward"] >= best), None)
        t_pass = next((p["t_sec"] for p in pts if p["passed"]), None)
        q = {f"r{int(f*100)}": step_value(pts, f * budget) for f in (0.25, 0.5, 0.75, 1.0)}
        summary.append({"cond": cond, "task": task, "submissions": len(subs),
                        "first_sub_sec": subs[0]["t_sec"] if subs else None, **q,
                        "t_best_sec": t_best, "t_pass_sec": t_pass, "auc": round(auc, 4),
                        "final": pts[-1]["reward"] if pts else None,
                        "cost_at_best": next((p["cum_cost_usd"] for p in pts if p["reward"] >= best), None),
                        "turn_at_best": next((p["turn"] for p in pts if p["reward"] >= best), None)})

    if a.out:
        out = Path(a.out)
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(points[0].keys()) if points else ["cond"]); w.writeheader(); w.writerows(points)
        cpath = out.with_name(out.stem + "_curve.csv")
        with cpath.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["cond", "task", "trial", "t_sec", "reward"]); w.writeheader(); w.writerows(curve_rows)
        print(f"wrote {out} ({len(points)} points), {cpath} ({len(curve_rows)} samples)", file=sys.stderr)

    cols = ["cond", "task", "submissions", "first_sub_sec", "r25", "r50", "r75", "r100", "t_best_sec", "t_pass_sec",
            "auc", "final", "cost_at_best", "turn_at_best"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for s in summary:
        lines.append("| " + " | ".join("" if s[c] is None else (f"{s[c]:.3f}" if isinstance(s[c], float) else str(s[c])) for c in cols) + " |")
    # condition means
    lines.append("")
    lines.append("| cond | tasks | mean auc | mean r50 | mean final | mean t_best (s) |")
    lines.append("|---|---|---|---|---|---|")
    for cond in sorted({s["cond"] for s in summary}):
        ss = [s for s in summary if s["cond"] == cond]
        tb = [s["t_best_sec"] for s in ss if s["t_best_sec"] is not None]
        lines.append(f"| {cond} | {len(ss)} | {fmean(s['auc'] for s in ss):.3f} | {fmean(s['r50'] for s in ss):.3f} | "
                     f"{fmean(s['final'] or 0 for s in ss):.3f} | {fmean(tb):.0f} |" if tb else f"| {cond} | {len(ss)} | | | | |")
    text = "\n".join(lines)
    print(text)
    if a.md:
        Path(a.md).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
