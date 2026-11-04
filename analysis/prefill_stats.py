#!/usr/bin/env python3
"""Post-compaction prefill statistics from token_turns tables.

    python analysis/prefill_stats.py jobs/<job>... [--md out.md] [--json out.json]

For every compaction c (first main turn whose prompt is the compacted context):
  L_before   prompt at turn c-1 (effective compaction limit)
  L_p        prompt at turn c (compacted context)
  read_c     cache read at turn c (part of the new prefix still cached, e.g. the system prompt)
  write_c    cache write at turn c = prefill of the new prefix
             (cache_write when recorded, else prompt - cache_read)
  write_5    cache write summed over turns c..c+4 (new prefix + first re-exploration turns)
  base_write median per-turn cache write over the 20 turns before c (steady-state prefill)
  summ_in/out tokens of the summarization calls of this compaction
Prints per-compaction rows and mean/std/CV per (model, condition, task), per (model,
condition) and per (model, task). The JSON is what analysis/chart.py embeds.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def short_task(task: str) -> str:
    return task.split("__")[0]


def load_job(job_dir: Path) -> tuple[dict, list[dict]]:
    tt = job_dir / "token_turns"
    with (tt / "summary.csv").open() as f:
        meta = {r["trial"]: r for r in csv.DictReader(f)}
    with (tt / "turns.csv").open() as f:
        turns = list(csv.DictReader(f))
    return meta, turns


def write_of(r: dict) -> float:
    w = _num(r.get("cache_write"))
    if w is None:
        w = max(_int(r["prompt_tokens"]) - _int(r["cache_read"]), 0)
    return w


def events_of(job_dir: Path) -> list[dict]:
    meta, turns = load_job(job_dir)
    events = []
    for trial, m in meta.items():
        rows = [r for r in turns if r["trial"] == trial]
        main = {_int(r["turn"]): r for r in rows if r["kind"] == "main"}
        summ = [r for r in rows if r["kind"] == "summarization"]
        comps = sorted(t for t, r in main.items() if _int(r.get("compaction")) == 1)
        for n, c in enumerate(comps, start=1):
            if c - 1 not in main:
                continue
            prev, cur = main[c - 1], main[c]
            base = [write_of(main[k]) for k in range(max(1, c - 20), c) if k in main]
            w5 = [write_of(main[k]) for k in range(c, c + 5) if k in main]
            cb = [_int(main[k]["completion_tokens"]) for k in range(max(1, c - 5), c) if k in main]
            ca = [_int(main[k]["completion_tokens"]) for k in range(c, c + 5) if k in main]
            calls = [r for r in summ if r["file"].startswith(f"trajectory.summarization-{n}-")]
            events.append({
                "job": job_dir.name, "model": m["model"], "cond": m["condition"],
                "task": short_task(m["task"]), "trial": trial, "turn": c,
                "L_before": _int(prev["prompt_tokens"]), "L_p": _int(cur["prompt_tokens"]),
                "read_c": _int(cur["cache_read"]), "write_c": write_of(cur), "write_5": sum(w5),
                "base_write": st.median(base) if base else None,
                "summ_in": sum(_int(r["prompt_tokens"]) for r in calls),
                "summ_out": sum(_int(r["completion_tokens"]) for r in calls),
                "completion_c": _int(cur["completion_tokens"]),
                "out_before5": st.mean(cb) if cb else None, "out_after5": st.mean(ca) if ca else None,
            })
    return events


def agg(rows: list[dict], key: str):
    v = [r[key] for r in rows if r.get(key) is not None]
    if not v:
        return None
    mu = st.mean(v)
    sd = st.pstdev(v) if len(v) > 1 else 0.0
    return {"n": len(v), "mean": mu, "std": sd, "cv": sd / mu if mu else None, "min": min(v), "max": max(v)}


def f(x):
    return "-" if x is None else (f"{x:,.0f}" if abs(x) >= 100 else f"{x:.2f}")


KEYS = ("write_c", "write_5", "L_p", "L_before", "base_write", "summ_in", "summ_out",
        "out_before5", "completion_c", "out_after5")


def group(events: list[dict], keyf, title: str, lines: list[str]) -> dict:
    g: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        g[keyf(e)].append(e)
    lines.append(f"\n## {title}\n")
    hdr = ["group", "n", "L_before mean", "L_p mean±std", "write_c mean", "write_c std", "write_c CV",
           "write_c min", "write_c max", "write_5 mean", "write_5 CV", "base_write", "summ_in mean",
           "summ_out mean", "out_before5", "out_c", "out_after5"]
    lines.append("| " + " | ".join(hdr) + " |")
    lines.append("|" + "---|" * len(hdr))
    out = {}
    for k in sorted(g):
        r = g[k]
        a = {key: agg(r, key) for key in KEYS}
        out[k] = {"n": len(r), "write_c": a["write_c"], "write_5": a["write_5"], "L_p": a["L_p"],
                  "L_before": a["L_before"], "base_write": a["base_write"], "summ_in": a["summ_in"],
                  "summ_out": a["summ_out"], "out_before5": a["out_before5"], "out_c": a["completion_c"],
                  "out_after5": a["out_after5"]}
        wc, w5, lp, lb = a["write_c"], a["write_5"], a["L_p"], a["L_before"]
        lines.append("| " + " | ".join([
            k, str(len(r)), f(lb["mean"]), f"{f(lp['mean'])}±{f(lp['std'])}", f(wc["mean"]), f(wc["std"]),
            f(wc["cv"]), f(wc["min"]), f(wc["max"]), f(w5["mean"]), f(w5["cv"]),
            f(a["base_write"]["mean"]) if a["base_write"] else "-", f(a["summ_in"]["mean"]),
            f(a["summ_out"]["mean"]), f(a["out_before5"]["mean"]) if a["out_before5"] else "-",
            f(a["completion_c"]["mean"]), f(a["out_after5"]["mean"]) if a["out_after5"] else "-",
        ]) + " |")
    return out


def compute(job_dirs: list[Path], cond_of: dict[str, str] | None = None) -> tuple[dict, str]:
    """cond_of: optional {job dir name: condition label} override (analysis/chart.py --cond)."""
    events: list[dict] = []
    for d in job_dirs:
        if (d / "token_turns" / "turns.csv").exists():
            for e in events_of(d):
                if cond_of and d.name in cond_of:
                    e["cond"] = cond_of[d.name]
                events.append(e)
        else:
            print(f"skip {d}: no token_turns/turns.csv (run analysis/token_turns.py first)", file=sys.stderr)
    lines = ["# Post-compaction prefill (cache write at the first compacted turn)\n", "## Every compaction\n"]
    hdr = ["job", "task", "turn", "L_before", "L_p", "read_c", "write_c", "write_5", "base_write",
           "summ_in", "summ_out", "completion_c", "out_before5", "out_after5"]
    lines.append("| " + " | ".join(hdr) + " |")
    lines.append("|" + "---|" * len(hdr))
    for e in events:
        lines.append("| " + " | ".join(f(e[h]) if isinstance(e[h], (int, float)) else str(e[h]) for h in hdr) + " |")
    res = {"events": events}
    res["by_cond_task"] = group(events, lambda e: f"{e['model']}/{e['cond']}/{e['task']}", "Per condition x task", lines)
    res["by_cond"] = group(events, lambda e: f"{e['model']}/{e['cond']}", "Per condition (tasks pooled)", lines)
    res["by_task"] = group(events, lambda e: f"{e['model']}/{e['task']}", "Per model x task (conditions pooled)", lines)
    return res, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job_dirs", type=Path, nargs="+")
    ap.add_argument("--md")
    ap.add_argument("--json")
    a = ap.parse_args()
    res, txt = compute(a.job_dirs)
    print(txt)
    if a.md:
        Path(a.md).write_text(txt + "\n")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
