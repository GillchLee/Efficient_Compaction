#!/usr/bin/env python3
"""Interactive per-turn chart (prompt / cache read / cache write / output, compactions,
rejections, interim rewards) for every trial of the given jobs, as one HTML file.

    python analysis/chart.py jobs/<job>... [--out jobs/chart.html]
                             [--public jobs/public_feedback.json] [--warn "note shown on top"]

Reads <job>/token_turns/{turns,summary,submissions}.csv (analysis/token_turns.py) and
embeds analysis/prefill_stats.py numbers; prices come from configs/prices.json (row =
model id without the provider prefix). Conditions are (model, condition) pairs from
summary.csv, tasks are the trial task names without their __NN suffix.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prefill_stats  # noqa: E402

CONFIG_DIR = HERE.parent / "configs"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def short_model(model: str) -> str:
    m = re.sub(r"^(claude|gpt)-", "", model)
    return re.sub(r"-\d+(-\d+)*$", "", m) or model


def trial_entries(job_dir: Path, public: dict) -> list[dict]:
    tt = job_dir / "token_turns"
    with (tt / "summary.csv").open() as f:
        metas = list(csv.DictReader(f))
    with (tt / "turns.csv").open() as f:
        turns = list(csv.DictReader(f))
    subs: list[dict] = []
    if (tt / "submissions.csv").exists():
        with (tt / "submissions.csv").open() as f:
            subs = list(csv.DictReader(f))
    out = []
    for m in metas:
        trial = m["trial"]
        rows = [r for r in turns if r["trial"] == trial]
        main = [r for r in rows if r["kind"] == "main"]
        if not main or not m["end"]:   # no result.json: cancelled or still running
            continue
        summ = [r for r in rows if r["kind"] == "summarization"]
        comps = [_int(r["turn"]) for r in main if _int(r.get("compaction")) == 1]
        data_rows = []
        n_comp = 0
        for r in main:
            si = so = 0
            if _int(r.get("compaction")) == 1:
                n_comp += 1
                calls = [c for c in summ if c["file"].startswith(f"trajectory.summarization-{n_comp}-")]
                si = sum(_int(c["prompt_tokens"]) for c in calls)
                so = sum(_int(c["completion_tokens"]) for c in calls)
            data_rows.append([_int(r["turn"]), _int(r["prompt_tokens"]), _int(r["cache_read"]),
                              int(prefill_stats.write_of(r)), _int(r["completion_tokens"]), si, so])
        reward = _num(m["reward"])
        rcurve = [[_int(s["turn"]), _num(s["reward"]), s["submission"]]
                  for s in subs if s["trial"] == trial and _num(s["reward"]) is not None and s["turn"] != ""]
        if reward is not None:
            rcurve.append([_int(main[-1]["turn"]), reward, "final"])
        pub = public.get(f"{job_dir.name}/{trial}") or []
        pub_max = max((c for _, c in pub), default=None)
        stamps = [_ts(r["timestamp"]) for r in main if _ts(r["timestamp"])]
        out.append({
            "job": job_dir.name, "model": m["model"], "cond": m["condition"],
            "task": prefill_stats.short_task(m["task"]), "trial": trial, "reward": reward,
            "n": len(main), "max_prompt": max(r[1] for r in data_rows),
            "dur_min": (max(stamps) - min(stamps)) / 60 if len(stamps) > 1 else None,
            "out_sum": sum(r[4] for r in data_rows), "comp": comps,
            "rej": [_int(r["turn"]) for r in main if _int(r.get("rejected")) == 1],
            "phases": _int(m["phases"], None), "end": m["end"] or "?",
            "first_done_min": _num(m["first_completion_min"]),
            "cmd_turns": _int(m["cmd_turns"]), "rcurve": rcurve,
            "pub6": next((t for t, c in pub if c >= pub_max), None) if pub_max else None,
            "first_ver": next((_int(r["turn"]) for r in main if _int(r.get("rejected")) == 1), None),
            "ol_limit": [[_int(r["turn"]), _int(r["ol_limit"])] for r in main if r.get("ol_limit")],
            "rows": data_rows,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job_dirs", type=Path, nargs="+")
    ap.add_argument("--out", type=Path, default=Path("jobs/chart.html"))
    ap.add_argument("--public", type=Path, help="analysis/public_feedback.py output")
    ap.add_argument("--warn", default="", help="note shown at the top of the page")
    ap.add_argument("--tpl", type=Path, default=HERE / "chart.tpl.html")
    ap.add_argument("--cond", action="append", default=[], metavar="JOB=LABEL",
                    help="condition label for that job directory name (default: summary.csv condition)")
    ap.add_argument("--intro", type=Path, help="HTML file inserted above the charts (tables, notes)")
    ap.add_argument("--title", default="LHTB Turn Cache Profile")
    a = ap.parse_args()
    cond_of = dict(kv.split("=", 1) for kv in a.cond)

    public = json.loads(a.public.read_text()) if a.public and a.public.exists() else {}
    data: list[dict] = []
    jobs = []
    for d in a.job_dirs:
        if not (d / "token_turns" / "summary.csv").exists():
            print(f"skip {d}: no token_turns/summary.csv (run analysis/token_turns.py first)", file=sys.stderr)
            continue
        jobs.append(d)
        for e in trial_entries(d, public):
            if d.name in cond_of:
                e["cond"] = cond_of[d.name]
            data.append(e)
    if not data:
        print("no trials", file=sys.stderr)
        return 1
    stats, _ = prefill_stats.compute(jobs, cond_of)

    table = json.loads((CONFIG_DIR / "prices.json").read_text())["models"]
    price = {}
    for m in sorted({d["model"] for d in data}):
        e = table.get(m) or {}
        price[m] = {"read": e.get("read", 0), "write": e.get("write", e.get("uncached", 0)),
                    "unc": e.get("uncached", e.get("write", 0)), "out": e.get("output", 0)}
        if not e:
            print(f"warning: no price row for {m} in configs/prices.json (costs shown as 0)", file=sys.stderr)
    conds, seen = [], set()
    for d in data:
        k = (d["model"], d["cond"])
        if k not in seen:
            seen.add(k)
            conds.append([d["model"], d["cond"],
                          d["cond"] if d["job"] in cond_of else f"{short_model(d['model'])} {d['cond']}"])
    tasks = [[t, t] for t in sorted({d["task"] for d in data})]

    html = (a.tpl.read_text()
            .replace("__DATA__", json.dumps(data, separators=(",", ":")))
            .replace("__STATS__", json.dumps(stats, separators=(",", ":")))
            .replace("__PRICE__", json.dumps(price))
            .replace("__CONDS__", json.dumps(conds, ensure_ascii=False))
            .replace("__TASKS__", json.dumps(tasks))
            .replace("__WARN__", json.dumps(a.warn, ensure_ascii=False))
            .replace("__TITLE__", a.title)
            .replace("__INTRO__", a.intro.read_text() if a.intro else ""))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(html)
    print(f"{a.out}: {len(html):,} bytes, {len(data)} trials, {len(conds)} conditions, {len(tasks)} tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
