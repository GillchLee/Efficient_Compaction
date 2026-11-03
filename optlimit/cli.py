"""optlimit command line.

  optlimit optimum  --Lp 8000 --g 1400 --L0 900 --prices qwen3.8-flash [--model-b] ...
  optlimit serve    --port 8300 --openai-upstream http://127.0.0.1:8600 --trigger error --Lp 8000 ...
  optlimit sessions --log proxy.jsonl
  optlimit analyze  --log proxy.jsonl [--job-dir tb_jobs/<job>] --prices <name> [--out result.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import cost_model as cm
from .controller import ControllerConfig

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


def load_json(name: str) -> dict:
    with open(CONFIG_DIR / name) as f:
        return json.load(f)


def prices_by_name(name: str) -> cm.Prices:
    table = load_json("prices.json")["models"]
    if name not in table:
        raise SystemExit(f"unknown price entry {name!r}; known: {', '.join(table)}")
    e = table[name]
    return cm.Prices(read=e["read"], write=e["write"], output=e["output"], uncached=e.get("uncached"))


def _add_model_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--priors", help="harness key in configs/priors.json (fills Lp, g, L0, m, summary_out)")
    ap.add_argument("--prices", help="model key in configs/prices.json (sets alpha, beta)")
    ap.add_argument("--Lp", type=float)
    ap.add_argument("--g", type=float, help="g (optimum) / g prior (serve)")
    ap.add_argument("--L0", type=float)
    ap.add_argument("--alpha", type=float)
    ap.add_argument("--beta", type=float)
    ap.add_argument("--phi", type=float, default=0.0)
    ap.add_argument("--model-b", action="store_true", help="count the summary call (reads m*gamma*L, output beta*O_s)")
    ap.add_argument("--m", type=float, help="full-context reads per compaction (default from priors or 1)")
    ap.add_argument("--gamma", type=float, help="price multiplier of those reads: 1 cached, alpha uncached")
    ap.add_argument("--summary-out", type=float, help="summary output tokens (default: = Lp as in the derivation)")
    ap.add_argument("--summary-fixed", type=float, default=0.0)


def resolve_params(a) -> dict:
    """Merge priors / prices / explicit flags into cost-model parameters."""
    p: dict = {"Lp": None, "g": None, "L0": None, "alpha": None, "beta": 0.0, "phi": a.phi,
               "m": 0.0, "gamma": 1.0, "summary_out": None, "summary_fixed": a.summary_fixed}
    if a.priors:
        pri = load_json("priors.json")["harness"]
        if a.priors not in pri:
            raise SystemExit(f"unknown priors {a.priors!r}; known: {', '.join(pri)}")
        e = pri[a.priors]
        p.update({"Lp": e.get("Lp"), "g": e.get("g"), "L0": e.get("L0"), "m": e.get("m", 1.0),
                  "gamma": e.get("gamma", 1.0), "summary_out": e.get("summary_out")})
    if a.prices:
        pr = prices_by_name(a.prices)
        p["alpha"] = pr.alpha
        p["beta"] = pr.beta
    for k in ("Lp", "g", "L0", "alpha", "beta", "m", "gamma"):
        v = getattr(a, k)
        if v is not None:
            p[k] = v
    if a.summary_out is not None:
        p["summary_out"] = a.summary_out
    if not a.model_b:
        p["m"], p["beta"], p["summary_fixed"] = 0.0, 0.0, 0.0
    elif p["m"] == 0:
        p["m"] = 1.0
    for k in ("Lp", "g", "alpha"):
        if p[k] is None:
            raise SystemExit(f"--{k} (or --priors/--prices) is required")
    return p


def cmd_optimum(a) -> None:
    p = resolve_params(a)
    if p["L0"] is None:
        raise SystemExit("--L0 (or --priors) is required")
    params = cm.Params(L0=p["L0"], Lp=p["Lp"], g=p["g"], alpha=p["alpha"], beta=p["beta"], phi=p["phi"],
                       m=p["m"], gamma=p["gamma"], summary_out=p["summary_out"], summary_fixed=p["summary_fixed"])
    Lc = cm.optimal_L_closed(params) if params.phi == 0 else None
    Ln = cm.optimal_L(params)
    print(json.dumps({"params": params.__dict__, "L_closed": Lc, "L_numeric": Ln,
                      "breakdown_at_opt": cm.window_cost_breakdown(Ln, params)}, indent=1))
    if a.table:
        print(f"\n{'L':>9} {'K':>6} {'f':>10} {'f/f*':>6}")
        fstar = cm.f(Ln, params)
        for L in a.table:
            v = cm.f(L, params)
            print(f"{L:9.0f} {cm.turns_per_window(L, params):6.1f} {v:10.0f} {v / fstar:6.2f}")


def cmd_serve(a) -> None:
    import uvicorn
    from .proxy import ProxyConfig, create_app
    p = resolve_params(a) if (a.priors or a.prices or a.Lp) else None
    cc = ControllerConfig(mode=a.mode, fixed_limit=a.limit)
    if p:
        cc.Lp = p["Lp"]; cc.alpha = p["alpha"]; cc.beta = p["beta"]; cc.phi = p["phi"]
        cc.m = p["m"]; cc.gamma = p["gamma"]; cc.summary_out = p["summary_out"]; cc.summary_fixed = p["summary_fixed"]
        if p["g"] is not None:
            cc.g_prior = p["g"]
        if a.L0 is not None or (a.priors and p["L0"] is not None and a.L0_mode == "fixed"):
            cc.L0_fixed = p["L0"]
    if a.mode == "fixed" and a.limit is None:
        raise SystemExit("--mode fixed needs --limit")
    cc.L0_mode = a.L0_mode
    cc.g_prior_weight = a.g_prior_weight; cc.g_mode = a.g_mode; cc.g_clip = a.g_clip
    cc.limit_min = a.limit_min; cc.limit_max = a.limit_max; cc.min_window_turns = a.min_window_turns
    cc.lookahead = a.lookahead; cc.horizon = a.horizon
    pc = ProxyConfig(anthropic_upstream=a.anthropic_upstream, openai_upstream=a.openai_upstream,
                     trigger=a.trigger, signal_high=a.signal_high, signal_low_cap=a.signal_low_cap,
                     error_limit_tokens=a.error_limit_tokens, log_path=a.log, dump_dir=a.dump_dir,
                     dump_messages=a.dump_messages, upstream_timeout=a.upstream_timeout,
                     inject_anthropic_key=os.environ.get(a.inject_anthropic_key_env) if a.inject_anthropic_key_env else None,
                     inject_openai_key=os.environ.get(a.inject_openai_key_env) if a.inject_openai_key_env else None,
                     extra_summary_prefixes=tuple(a.summary_prefix or ()),
                     model_rewrite=dict(x.split('=', 1) for x in (a.model_rewrite or [])), controller=cc)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("optlimit")
    logger.info("controller: %s", json.dumps(cc.__dict__))
    logger.info("proxy: trigger=%s anthropic=%s openai=%s log=%s", pc.trigger, pc.anthropic_upstream,
                pc.openai_upstream, pc.log_path)
    uvicorn.run(create_app(pc), host=a.host, port=a.port, log_level="warning", timeout_keep_alive=600)


def cmd_sessions(a) -> None:
    from .analysis import load_log, sessions_table
    recs = load_log(a.log)
    for row in sessions_table(recs):
        print(json.dumps(row, ensure_ascii=False))


def cmd_analyze(a) -> None:
    from .analysis import analyze
    prices = prices_by_name(a.prices) if a.prices else None
    out = analyze(a.log, a.job_dir, prices, summary_prices=prices_by_name(a.summary_prices) if a.summary_prices else None)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    print(out["markdown"])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="optlimit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("optimum", help="print L* for given parameters")
    _add_model_args(o)
    o.add_argument("--table", type=float, nargs="*", help="also print f(L) at these L")
    o.set_defaults(fn=cmd_optimum)

    s = sub.add_parser("serve", help="run the trigger proxy")
    _add_model_args(s)
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8300)
    s.add_argument("--anthropic-upstream", default="https://api.anthropic.com")
    s.add_argument("--openai-upstream", default="https://api.openai.com")
    s.add_argument("--trigger", choices=["usage", "error", "off"], default="usage")
    s.add_argument("--mode", choices=["formula", "fixed", "off"], default="formula")
    s.add_argument("--limit", type=float, help="fixed threshold (tokens) for --mode fixed")
    s.add_argument("--L0-mode", choices=["first_prompt", "fixed", "cached_after_compaction"], default="first_prompt")
    s.add_argument("--g-prior-weight", type=float, default=5.0)
    s.add_argument("--g-mode", choices=["mean", "median", "ema"], default="mean")
    s.add_argument("--g-clip", type=float, default=10.0)
    s.add_argument("--limit-min", type=float)
    s.add_argument("--limit-max", type=float)
    s.add_argument("--min-window-turns", type=int, default=3)
    s.add_argument("--lookahead", type=float, default=1.0)
    s.add_argument("--horizon", type=int, help="expected total turns -> PDF 6(a) short-task veto")
    s.add_argument("--signal-high", type=int, default=950_000)
    s.add_argument("--signal-low-cap", type=int, default=50_000)
    s.add_argument("--error-limit-tokens", type=int, default=1_000_000)
    s.add_argument("--log", help="JSONL request log")
    s.add_argument("--dump-dir")
    s.add_argument("--dump-messages", action="store_true")
    s.add_argument("--upstream-timeout", type=float, default=3600.0)
    s.add_argument("--inject-anthropic-key-env", help="env var holding the real key to send upstream")
    s.add_argument("--model-rewrite", action="append", metavar="SRC=DST",
                   help="rewrite body.model SRC to DST before forwarding (e.g. glm-5.3-flash=z-ai/glm-5.3-flash for OpenRouter)")
    s.add_argument("--inject-openai-key-env")
    s.add_argument("--summary-prefix", action="append", help="extra last-user-message prefix marking a summary call")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_serve)

    q = sub.add_parser("sessions", help="summarise a proxy log per session")
    q.add_argument("--log", required=True)
    q.set_defaults(fn=cmd_sessions)

    z = sub.add_parser("analyze", help="join proxy log with a harbor job dir and price it")
    z.add_argument("--log", required=True)
    z.add_argument("--job-dir")
    z.add_argument("--prices")
    z.add_argument("--summary-prices", help="price entry for summary calls if a different model made them")
    z.add_argument("--out")
    z.set_defaults(fn=cmd_analyze)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
