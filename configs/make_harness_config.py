#!/usr/bin/env python3
"""Job configs for harnesses that compact on their own (Claude Code, Codex) with optlimit choosing
the moment. Usually reached through `configs/make_config.py --harness claude-code|codex` (same flags);
see docs/ARCHITECTURE.md and docs/other_harnesses.md.

    # static: the harness's own threshold, set at launch (no proxy; --observe logs usage)
    python configs/make_config.py --harness claude-code --model anthropic/claude-haiku-5-5 --limit 87k \
        --benchmark lhtb2 --tasks seismo_cross_correlation_lag__01
    # dynamic: optlimit proxy (balance = DTAP rule; PRIOR defaults to the harness's prior)
    python configs/make_config.py --harness codex --model openai/gpt-6-luna --balance --prices gpt-6-luna \
        --learn --benchmark tb4 --tasks coq-block-bound
    # control: the same L as --limit, but through the proxy trigger
    python configs/make_config.py --harness codex --model openai/gpt-6-luna --proxy-fixed 40k ...

Harness specifics (agent class, trigger, reporting band, minimum window, native threshold knob,
default prior) come from optlimit/harnesses.py; the benchmark from configs/benchmarks.yaml.
Run with scripts/run_job.sh (it starts the proxy when the config has a `# proxy:` line).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from _common import (add_benchmark_args, benchmark_extras, continue_env_line, harbor_header_line,  # noqa: E402
                     parse_sets, parse_tokens, repo_relative, resolve_benchmark, short_model)
from optlimit import harnesses  # noqa: E402

PROXY_HARNESSES = sorted(n for n, p in harnesses.PROFILES.items() if p.connection == "proxy")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--harness", choices=PROXY_HARNESSES, required=True)
    ap.add_argument("--model", required=True, help="litellm-style id, e.g. anthropic/claude-sonnet-5-5, openai/gpt-6-luna")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--limit", help="static native threshold N tokens, or 'native' (harness default)")
    g.add_argument("--balance", nargs="?", const="", metavar="PRIOR",
                   help="optlimit balance (DTAP) mode via the proxy (PRIOR default: the harness's)")
    g.add_argument("--formula", nargs="?", const="", metavar="PRIOR", help="optlimit formula mode via the proxy")
    g.add_argument("--proxy-fixed", metavar="N",
                   help="fixed L enforced through the proxy (control: same L as --limit, dynamic trigger path)")
    ap.add_argument("--prices", help="configs/prices.json row (alpha, beta) for --balance/--formula")
    ap.add_argument("--learn", action="store_true",
                    help="learn Lp, summary output and gamma from this run's own compactions")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="extra controller field")
    ap.add_argument("--trigger", choices=["usage", "error"], help="proxy trigger (default: the harness profile's)")
    ap.add_argument("--observe", action="store_true",
                    help="static modes: still run the proxy with trigger=off to log every call's usage")
    add_benchmark_args(ap)
    ap.add_argument("--env", choices=["docker", "daytona"], default="docker")
    ap.add_argument("--concurrent", type=int, default=1)
    ap.add_argument("--verifier-feedback", choices=["true", "false", "score"])
    ap.add_argument("--job-name")
    ap.add_argument("--jobs-dir", default="./jobs")
    ap.add_argument("--out")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    prof = harnesses.get(a.harness)
    priors = json.loads((HERE / "priors.json").read_text())["harness"]
    prices = json.loads((HERE / "prices.json").read_text())["models"]
    band = prof.band
    trigger = a.trigger or prof.trigger

    def proxy_block(spec: dict) -> dict:
        spec = {**spec, **parse_sets(a.set)}
        spec.setdefault("min_window_turns", prof.min_window_turns)
        return {"trigger": trigger, "spec": spec, "low_cap": band["low_cap"], "high": band["high"],
                "min_window_turns": spec["min_window_turns"]}

    proxy: dict | None = None
    if a.limit is not None:
        if a.limit == "native":
            cond, kwargs = "native", (prof.proxy_kwargs() if prof.agent_kwargs else {})
        else:
            n = parse_tokens(a.limit)
            try:
                cond, kwargs = f"l{n // 1000}k", prof.native_kwargs(n)
            except ValueError as e:
                ap.error(str(e))
        if a.observe:
            proxy = {"trigger": "off", "spec": {"mode": "off"}}
    elif a.proxy_fixed is not None:
        n = parse_tokens(a.proxy_fixed)
        cond, kwargs = f"proxyl{n // 1000}k", prof.proxy_kwargs()
        proxy = proxy_block({"mode": "fixed", "fixed_limit": n, "lookahead": 0})
    else:
        mode = "balance" if a.balance is not None else "formula"
        prior = (a.balance if mode == "balance" else a.formula) or prof.default_prior
        if prior not in priors:
            ap.error(f"{prior!r} is not in configs/priors.json (known: {', '.join(sorted(priors))})")
        if a.prices and a.prices not in prices:
            ap.error(f"--prices {a.prices!r} is not in configs/prices.json (known: {', '.join(sorted(prices))})")
        spec: dict = {"mode": mode, "prior": prior, "lookahead": 1 if mode == "formula" else 0,
                      "limit_max": band["limit_max"]}
        if a.prices:
            spec["prices"] = a.prices
        if a.learn:
            spec.update(learn_lp=True, learn_summary_out=True, learn_gamma=True)
        cond, kwargs = f"{mode}_{prior}", prof.proxy_kwargs()
        proxy = proxy_block(spec)

    dataset, hours, cont, bench = resolve_benchmark(a)
    harbor, harness_kwargs = benchmark_extras(a)
    kwargs = {**kwargs, **(harness_kwargs.get(a.harness) or {})}
    job = a.job_name or f"{a.harness}-{short_model(a.model)}_{cond}"
    out = Path(a.out) if a.out else HERE / "sweep" / f"{job}.yaml"
    agent = {**prof.agent, "model_name": a.model, "override_timeout_sec": int(hours * 3600), "kwargs": kwargs}
    env: dict = {"type": a.env, "force_build": False, "delete": True}
    if a.env == "daytona":
        env["kwargs"] = {"auto_stop_interval_mins": int(hours * 60) + 60, "auto_delete_interval_mins": 30}
    cfg = {"job_name": job, "jobs_dir": a.jobs_dir, "n_attempts": 1, "n_concurrent_trials": a.concurrent,
           "timeout_multiplier": 1.0, "environment": env, "agents": [agent], "datasets": [dataset]}
    env_lines = [ln for ln in [continue_env_line(cont or "same_conversation")] if ln]
    if a.verifier_feedback:
        env_lines.append(f"HB_VERIFIER_FEEDBACK={a.verifier_feedback}")
    header = (f"# generated by configs/make_config.py: harness={a.harness} model={a.model} condition={cond} "
              f"benchmark={bench} env={a.env} budget={hours}h tasks={a.tasks or 'all'}\n"
              f"# env: {' '.join(env_lines)}\n"
              + harbor_header_line(harbor)
              + (f"# proxy: {json.dumps(proxy, separators=(',', ':'))}\n" if proxy else "")
              + "# run: bash scripts/run_job.sh " + repo_relative(out) + "\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
