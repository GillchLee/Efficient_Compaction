#!/usr/bin/env python3
"""Fit the cost-model parameters of one model/harness from measured runs.

    python analysis/fit_priors.py jobs/<job>... [--prices claude-sonnet-5-5]
                                  [--name terminus2-sonnet] [--write configs/priors.json]

From token_turns tables (analysis/token_turns.py), grouped by model:
  g            prompt growth per turn: median (prior) and mean of prompt_delta over main
               turns that are not the first of a window
  L0           cached part of the post-compaction prompt: median cache_read at compaction turns
  Lp           written part of the post-compaction prompt: median cache_write at compaction
               turns (L0 + Lp = prompt right after compaction, as the cost model defines it)
  phi, c       Lp = c + phi * L_before, least squares over compactions when the runs cover
               at least two window sizes (otherwise phi = 0, c = Lp)
  m            summarization input per compaction / L_before (median)
  gamma        price multiplier of that input relative to a cache read (needs --prices)
  summary_out  summarization output tokens per compaction (median)
Prints the fit; --write merges it into configs/priors.json under "harness"/<name>.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prefill_stats  # noqa: E402

CONFIG_DIR = HERE.parent / "configs"


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _q(v, p):
    v = sorted(v)
    if not v:
        return None
    i = (len(v) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def fit(job_dirs: list[Path], prices: str | None) -> dict:
    alpha = None
    if prices:
        e = json.loads((CONFIG_DIR / "prices.json").read_text())["models"][prices]
        alpha = (e.get("uncached") or e["write"]) / e["read"]
    deltas: dict[str, list[float]] = defaultdict(list)
    trials: dict[str, set] = defaultdict(set)
    first_prompt: dict[str, list[int]] = defaultdict(list)
    events: dict[str, list[dict]] = defaultdict(list)
    for d in job_dirs:
        tt = d / "token_turns"
        if not (tt / "turns.csv").exists():
            print(f"skip {d}: no token_turns/turns.csv", file=sys.stderr)
            continue
        meta, turns = prefill_stats.load_job(d)
        ev_by_trial: dict[str, list[dict]] = defaultdict(list)
        for e in prefill_stats.events_of(d):   # in turn order per trial
            ev_by_trial[e["trial"]].append(e)
        for trial, m in meta.items():
            model = m["model"]
            trials[model].add(trial)
            main = [r for r in turns if r["trial"] == trial and r["kind"] == "main"]
            if main:
                first_prompt[model].append(_int(main[0]["prompt_tokens"]))
            for r in main:
                if _int(r["turn"]) > 1 and _int(r.get("compaction")) != 1:
                    deltas[model].append(float(r["prompt_delta"]))
            summ = [r for r in turns if r["trial"] == trial and r["kind"] == "summarization"]
            for n, e in enumerate(ev_by_trial.get(trial, []), start=1):
                calls = [r for r in summ if r["file"].startswith(f"trajectory.summarization-{n}-")]
                e["summ_read"] = sum(_int(r["cache_read"]) for r in calls)
                events[model].append(e)
    out = {}
    for model in sorted(trials):
        dl = deltas[model]
        ev = events[model]
        res: dict = {
            "source": f"fit_priors.py over {len(trials[model])} trials, {len(ev)} compactions",
            "trials": len(trials[model]), "compactions": len(ev),
            "g": round(st.median(dl)) if dl else None,
            "g_mean": round(st.mean(dl)) if dl else None,
            "g_q25": round(_q(dl, 0.25)) if dl else None, "g_q75": round(_q(dl, 0.75)) if dl else None,
            "first_prompt": round(st.median(first_prompt[model])) if first_prompt[model] else None,
        }
        if ev:
            lb = [e["L_before"] for e in ev]
            lp_full = [e["L_p"] for e in ev]
            wc = [e["write_c"] for e in ev]
            rc = [e["read_c"] for e in ev]
            res.update({
                "L0": round(st.median(rc)), "Lp": round(st.median(wc)),
                "Lp_full": round(st.median(lp_full)), "L_before_median": round(st.median(lb)),
                "m": round(st.median([e["summ_in"] / e["L_before"] for e in ev if e["L_before"]]), 3),
                "summary_out": round(st.median([e["summ_out"] for e in ev])),
            })
            # Lp = c + phi * L: only when the compactions span different limits
            levels = sorted({round(x / 10000) for x in lb})
            if len(ev) >= 6 and len(levels) >= 2:
                mx, my = st.mean(lb), st.mean(wc)
                sxx = sum((x - mx) ** 2 for x in lb)
                phi = sum((x - mx) * (y - my) for x, y in zip(lb, wc)) / sxx if sxx else 0.0
                phi = min(max(phi, 0.0), 0.5)
                res.update({"phi": round(phi, 4), "Lp_c": round(my - phi * mx)})
            else:
                res.update({"phi": 0.0, "Lp_c": res["Lp"]})
            tot = sum(e["summ_in"] for e in ev)
            read = sum(e["summ_read"] for e in ev)
            frac_uncached = (tot - read) / tot if tot else None
            res["summ_uncached_frac"] = round(frac_uncached, 3) if frac_uncached is not None else None
            if alpha is not None and frac_uncached is not None:
                res["gamma"] = round(1 + (alpha - 1) * frac_uncached, 3)
            else:
                res["gamma"] = 1.0
        out[model] = res
    return out


def to_prior(res: dict) -> dict:
    """priors.json entry (keys the controller reads) from a fit."""
    return {
        "source": res["source"],
        "Lp": res.get("Lp_c", res.get("Lp")), "L0": res.get("L0", 0), "g": res["g"],
        "m": res.get("m", 0), "gamma": res.get("gamma", 1.0), "summary_out": res.get("summary_out"),
        "phi": res.get("phi", 0.0),
        "_detail": (f"L0 = median cache read at the first compacted turn, Lp = median cache write there "
                    f"(full post-compaction prompt {res.get('Lp_full')}); g median {res['g']} / mean "
                    f"{res['g_mean']} (q25 {res['g_q25']}, q75 {res['g_q75']}); L_before median "
                    f"{res.get('L_before_median')}; summarization input {res.get('m')} x L, uncached "
                    f"fraction {res.get('summ_uncached_frac')}; first prompt {res.get('first_prompt')}."),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job_dirs", type=Path, nargs="+")
    ap.add_argument("--prices", help="configs/prices.json row, for gamma")
    ap.add_argument("--name", help="priors.json entry name (default terminus2-<model>)")
    ap.add_argument("--write", type=Path, help="merge into this priors.json")
    a = ap.parse_args()
    fits = fit(a.job_dirs, a.prices)
    if not fits:
        print("no data", file=sys.stderr)
        return 1
    for model, res in fits.items():
        print(f"== {model}")
        for k, v in res.items():
            print(f"  {k:<20} {v}")
    if a.write:
        doc = json.loads(a.write.read_text()) if a.write.exists() else {"harness": {}}
        for model, res in fits.items():
            name = a.name if (a.name and len(fits) == 1) else f"terminus2-{model}"
            doc.setdefault("harness", {})[name] = to_prior(res)
            print(f"-> {a.write}: harness/{name}")
        a.write.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
