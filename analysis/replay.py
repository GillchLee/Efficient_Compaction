#!/usr/bin/env python3
"""Replay recorded trials through the optlimit controller and price each compaction policy.

    python analysis/replay.py jobs/<job>... --prior terminus2-sonnet --prices claude-sonnet-5-5
                              [--limits 40000 80000 120000] [--no-formula] [--set key=value]
                              [--out jobs/replay.csv]

For every trial the observed per-turn prompt increments (prompt_delta of the main turns;
the increment at a real compaction turn is unknown and replaced by the prior g) and
output tokens are replayed under each policy:
  nocompact        the prompt only grows
  fixed:<L>        compact when the prompt reaches L (what optlimit.harbor_agent does in
                   mode fixed, lookahead 0)
  formula          L* chosen online by the controller (mode formula, lookahead 1) from the
                   prior and the prices
After a simulated compaction the prompt restarts at L0 + Lp(L) and the summarization is
charged m * L input at gamma x the cache-read price plus summary_out output tokens.
Per turn the previous prompt is a cache read and the increment a cache write.

Columns: cost_usd and cost_per_turn per policy, plus real_cost_* = the trial's own
accounting (cache read / write / uncached / output of every call) under the same prices,
so a fixed:<L> replay of a run that really used window L checks the cost model itself.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import prefill_stats  # noqa: E402
from optlimit.controller import ControllerConfig, LimitController, config_from_spec  # noqa: E402
from optlimit.cost_model import Prices  # noqa: E402

CONFIG_DIR = HERE.parent / "configs"
FIELDS = ["job", "trial", "task", "model", "condition", "turns", "policy", "compactions",
          "limit_mean", "read", "write", "out", "summ_in", "summ_out", "cost_usd",
          "cost_per_turn", "real_cost_usd", "real_cost_per_turn"]


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def load_prices(name: str) -> Prices:
    e = json.loads((CONFIG_DIR / "prices.json").read_text())["models"][name]
    return Prices(read=e["read"], write=e["write"], output=e["output"], uncached=e.get("uncached"))


def real_cost(rows: list[dict], pr: Prices) -> float:
    read = write = unc = out = 0
    for r in rows:
        p, cr = _int(r["prompt_tokens"]), _int(r["cache_read"])
        cw = _int(r["cache_write"]) if r.get("cache_write") not in ("", None) else None
        read += cr
        if cw is None:
            unc += max(p - cr, 0)
        else:
            write += cw
            unc += max(p - cr - cw, 0)
        out += _int(r["completion_tokens"])
    return pr.cost_usd(read, write, out, unc)


def simulate(policy: str, cfg: ControllerConfig, inc: list, outs: list[int], first: int,
             pr: Prices, shift: tuple[int, Prices] | None = None) -> dict:
    """Replay one trial under a policy.  `shift` = (turn, prices): from that turn on the
    tokens are billed at the new prices and the formula controller is told the new
    alpha/beta (update_prices), as the harbor agent does on a prices reload."""
    ctl = LimitController(cfg)
    key = "t"
    T = len(outs)
    P, read_t, write_t = first, 0, first
    read = write = out = summ_in = summ_out = 0.0
    read2 = write2 = out2 = summ_in2 = summ_out2 = 0.0   # after the shift
    comps = 0
    limits = []
    boundary = False
    shifted = False
    for t in range(T):
        if shift is not None and not shifted and t >= shift[0]:
            shifted = True
            ctl.update_prices(shift[1].alpha, shift[1].beta, note="replay shift")
        if shifted:
            read2 += read_t; write2 += write_t; out2 += outs[t]
        else:
            read += read_t; write += write_t; out += outs[t]
        d = ctl.on_prompt(key, "main", float(P), cached=float(read_t), window_start_hint=boundary)
        boundary = False
        if d.limit is not None:
            limits.append(d.limit)
        if t == T - 1:
            break
        if policy != "nocompact" and d.compact_now:
            p = ctl.params(ctl.session(key))
            lp = p.lp_at(P) if p else cfg.Lp
            m = (p.m if p else cfg.m) or 0.0
            gamma = p.gamma if p else cfg.gamma
            so = p.summary_out_at(P) if p else (cfg.summary_out or lp)
            if shifted:
                summ_in2 += m * P * gamma; summ_out2 += so
            else:
                summ_in += m * P * gamma; summ_out += so
            if cfg.learn_lp or cfg.learn_summary_out or cfg.learn_gamma:
                ctl.note_summary_usage(key, m * P, cached=m * P * (1 - (gamma - 1) / max(cfg.alpha - 1, 1e-9)), completion=so)
            comps += 1
            L0 = ctl.L0(ctl.session(key)) or 0.0
            read_t, write_t = L0, lp
            P = L0 + lp
            ctl.note_kind(key, "summary")
            boundary = True
        else:
            step = inc[t + 1] if inc[t + 1] is not None else cfg.g_prior
            read_t, write_t = P, max(step, 0.0)
            P = P + max(step, 0.0)
    cost = pr.cost_usd(read + summ_in, write, out + summ_out)
    if shift is not None:
        cost += shift[1].cost_usd(read2 + summ_in2, write2, out2 + summ_out2)
    return {"policy": policy, "compactions": comps,
            "limit_mean": round(st.mean(limits)) if limits else "",
            "read": round(read + read2), "write": round(write + write2), "out": round(out + out2),
            "summ_in": round(summ_in + summ_in2), "summ_out": round(summ_out + summ_out2),
            "cost_usd": round(cost, 4), "cost_per_turn": round(cost / T, 5)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job_dirs", type=Path, nargs="+")
    ap.add_argument("--prior", required=True, help="configs/priors.json harness entry")
    ap.add_argument("--prices", required=True, help="configs/prices.json row")
    ap.add_argument("--limits", type=int, nargs="*", default=[40000, 80000, 120000])
    ap.add_argument("--no-formula", action="store_true")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="controller field override for the formula policy")
    ap.add_argument("--price-shift", metavar="TURN:PRICES_ROW",
                    help="from TURN on, bill at this prices row and tell the formula controller "
                         "(fixed policies keep their window: the regret of not adapting)")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    pr = load_prices(a.prices)
    shift = None
    if a.price_shift:
        t_s, _, row = a.price_shift.partition(":")
        shift = (int(t_s), load_prices(row))
    base = {"prior": a.prior, "prices": a.prices}
    for kv in a.set:
        k, _, v = kv.partition("=")
        base[k] = json.loads(v) if v.replace(".", "", 1).lstrip("-").isdigit() or v in ("true", "false", "null") else v
    policies: list[tuple[str, ControllerConfig]] = [("nocompact", config_from_spec({**base, "mode": "off"}))]
    for L in a.limits:
        policies.append((f"fixed:{L}", config_from_spec({**base, "mode": "fixed", "fixed_limit": L, "lookahead": 0})))
    if not a.no_formula:
        policies.append(("formula", config_from_spec({**base, "mode": "formula", "lookahead": 1})))

    rows: list[dict] = []
    for d in a.job_dirs:
        if not (d / "token_turns" / "turns.csv").exists():
            print(f"skip {d}: no token_turns/turns.csv", file=sys.stderr)
            continue
        meta, turns = prefill_stats.load_job(d)
        for trial, m in meta.items():
            tr = [r for r in turns if r["trial"] == trial]
            main = sorted((r for r in tr if r["kind"] == "main"), key=lambda r: _int(r["turn"]))
            if len(main) < 2:
                continue
            inc = [None] + [None if _int(r.get("compaction")) == 1 else float(r["prompt_delta"]) for r in main[1:]]
            outs = [_int(r["completion_tokens"]) for r in main]
            rc = real_cost(tr, pr)
            for name, cfg in policies:
                sim = simulate(name, cfg, inc, outs, _int(main[0]["prompt_tokens"]), pr, shift)
                rows.append({"job": d.name, "trial": trial, "task": prefill_stats.short_task(m["task"]),
                             "model": m["model"], "condition": m["condition"], "turns": len(main), **sim,
                             "real_cost_usd": round(rc, 4), "real_cost_per_turn": round(rc / len(main), 5)})
    if not rows:
        print("no data", file=sys.stderr)
        return 1

    names = [n for n, _ in policies]
    print(f"{'trial':<44} {'cond':<9} {'turns':>5} {'real/turn':>10} " + " ".join(f"{n[:14]:>14}" for n in names))
    for trial in dict.fromkeys(r["trial"] for r in rows):
        tr = [r for r in rows if r["trial"] == trial]
        cells = []
        for n in names:
            r = next(x for x in tr if x["policy"] == n)
            cells.append(f"{r['cost_per_turn']:.5f}/{r['compactions']:>2}")
        print(f"{trial[:44]:<44} {tr[0]['condition']:<9} {tr[0]['turns']:>5} {tr[0]['real_cost_per_turn']:>10.5f} "
              + " ".join(f"{c:>14}" for c in cells))
    print("(cells: simulated $/turn / compactions)")
    if a.out:
        with a.out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
