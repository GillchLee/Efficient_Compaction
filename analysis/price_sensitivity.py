#!/usr/bin/env python3
"""How the cost-optimal compaction window L* depends on the price ratios.

    python analysis/price_sensitivity.py --prior terminus2-gpt-6-luna [--g 1021] [--out jobs/x.md]

alpha = cache-write (or uncached) price / cache-read price, beta = output / cache-read.
For the prior's Lp, L0, g, m, summary_out the script prints

1. the public price sheets (provider, model, cache tier) as (alpha, beta) with L*;
2. an (alpha, beta) grid of L* and of the per-turn cost f(L*) / g;
3. the regret of keeping the window tuned for one price sheet when the ratios change:
   f(L_ref; new prices) / f(L*_new; new prices) - 1.

gamma (price multiplier of the summary calls' reads) is re-derived from the prior's uncached
fraction u = (gamma_prior - 1) / (alpha_prior - 1) as 1 + (alpha - 1) u, as the controller
does with learn_gamma.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from optlimit.cost_model import Params, best_threshold, f  # noqa: E402

# Public list prices, $/MTok, read on 2026-11-04 (see docs/adaptive_limit_plan.md for sources).
# (read, write, output); write = cache write of the stated tier, or plain input when the API
# has no write premium.
PRICE_SHEETS = [
    ("anthropic", "claude-fable-5.1", "5m", 0.25, 12.5, 50),
    ("anthropic", "claude-fable-5.1", "1h", 0.25, 20.0, 50),
    ("anthropic", "claude-opus-5.5", "5m", 0.20, 5.0, 20),
    ("anthropic", "claude-opus-5.5", "1h", 0.20, 8.0, 20),
    ("anthropic", "claude-sonnet-5.5", "5m", 0.10, 2.5, 10),
    ("anthropic", "claude-sonnet-5.5", "1h", 0.10, 4.0, 10),
    ("anthropic", "claude-sonnet-5", "5m", 0.20, 2.5, 10),
    ("anthropic", "claude-sonnet-4.6", "5m", 0.30, 3.75, 15),
    ("anthropic", "claude-haiku-5.5", "5m", 0.01, 0.125, 0.5),
    ("anthropic", "claude-haiku-5.5", "1h", 0.01, 0.20, 0.5),
    ("openai", "gpt-6-luna", "input(no write)", 0.01, 0.10, 0.5),
    ("openai", "gpt-6-luna", "30m write", 0.01, 0.125, 0.5),
    ("openai", "gpt-6-luna", "12h write", 0.01, 0.25, 0.5),
    ("openai", "gpt-6-sol", "30m write", 0.20, 2.5, 10),
    ("openai", "gpt-6.1-sol", "30m write", 0.10, 2.5, 10),
    ("openai", "gpt-6.1-sol", "12h write", 0.10, 5.0, 10),
    ("openai", "gpt-6-astra", "30m write", 1.0, 12.5, 50),
    ("google", "gemini-3.8-flash", "input", 0.075, 0.75, 3.75),
    ("google", "gemini-3.1-pro", "input", 0.20, 2.0, 12),
    ("zhipu", "glm-5.3-flash", "input", 0.03, 0.0457, 0.0742),
]


def params(prior: dict, alpha: float, beta: float, g: float | None, u: float) -> Params:
    return Params(L0=float(prior["L0"]), Lp=float(prior["Lp"]), g=float(g or prior["g"]), alpha=alpha, beta=beta,
                  phi=0.0, m=float(prior.get("m") or 0), gamma=1 + (alpha - 1) * u,
                  summary_out=prior.get("summary_out"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prior", default="terminus2-gpt-6-luna")
    ap.add_argument("--g", type=float, help="override g (e.g. the observed median)")
    ap.add_argument("--alphas", type=float, nargs="*", default=[1.5, 5, 10, 12.5, 25, 50, 80])
    ap.add_argument("--betas", type=float, nargs="*", default=[2.5, 10, 50, 100, 200])
    ap.add_argument("--ref", default="10,50", help="reference alpha,beta the fixed window is tuned for")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    pri = json.loads((HERE.parent / "configs" / "priors.json").read_text())["harness"][a.prior]
    a0 = 10.0
    u = max(0.0, (float(pri.get("gamma") or 1) - 1) / (a0 - 1))   # priors were fitted at alpha = 10 (Luna) ...
    if "sonnet" in a.prior:
        u = max(0.0, (float(pri.get("gamma") or 1) - 1) / (12.5 - 1))
    lines: list[str] = []
    P = lines.append
    g = a.g or float(pri["g"])
    P(f"# L* vs price ratios — prior {a.prior} (Lp {pri['Lp']}, L0 {pri['L0']}, g {g:.0f}, m {pri.get('m')}, "
      f"summary_out {pri.get('summary_out')}, uncached fraction of summary reads u {u:.3f})\n")
    P("## 1. Public price sheets\n")
    P("| provider | model | cache tier | read | write | out | alpha | beta | gamma | L* | K | f(L*)/g |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for prov, model, tier, r, w, o in PRICE_SHEETS:
        al, be = w / r, o / r
        p = params(pri, al, be, g, u)
        L = best_threshold(p)
        K = (L - p.L0 - p.Lp) / p.g + 1
        P(f"| {prov} | {model} | {tier} | {r} | {w} | {o} | {al:.1f} | {be:.0f} | {p.gamma:.2f} | {L:,.0f} | {K:.0f} | {f(L, p) / g:.1f} |")
    P("\n## 2. L* grid (rows alpha, columns beta)\n")
    P("| alpha \\ beta | " + " | ".join(f"{b:g}" for b in a.betas) + " |")
    P("|---|" + "---|" * len(a.betas))
    for al in a.alphas:
        cells = []
        for be in a.betas:
            p = params(pri, al, be, g, u)
            cells.append(f"{best_threshold(p):,.0f}")
        P(f"| {al:g} | " + " | ".join(cells) + " |")
    ra, rb = (float(x) for x in a.ref.split(","))
    Lref = best_threshold(params(pri, ra, rb, g, u))
    P(f"\n## 3. Regret of a window tuned for alpha={ra:g}, beta={rb:g} (L_ref = {Lref:,.0f}) when the ratios change\n")
    P("per-turn cost of L_ref divided by the cost of the re-optimised window, minus 1\n")
    P("| alpha \\ beta | " + " | ".join(f"{b:g}" for b in a.betas) + " |")
    P("|---|" + "---|" * len(a.betas))
    for al in a.alphas:
        cells = []
        for be in a.betas:
            p = params(pri, al, be, g, u)
            Ls = best_threshold(p)
            cells.append(f"{f(Lref, p) / f(Ls, p) - 1:+.1%}")
        P(f"| {al:g} | " + " | ".join(cells) + " |")
    P("\n## 4. Elasticities at the reference point (d ln L* / d ln x)\n")
    p0 = params(pri, ra, rb, g, u)
    L0s = best_threshold(p0)
    for name, pp in (("alpha", params(pri, ra * 1.01, rb, g, u)), ("beta", params(pri, ra, rb * 1.01, g, u)),
                     ("g", params(pri, ra, rb, g * 1.01, u))):
        P(f"- {name}: {(best_threshold(pp) / L0s - 1) / 0.01:.2f}")
    text = "\n".join(lines)
    print(text)
    if a.out:
        a.out.write_text(text + "\n")
        print(f"-> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
