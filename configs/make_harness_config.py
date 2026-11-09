#!/usr/bin/env python3
"""Job configs for harnesses that compact on their own (Claude Code, Codex) with optlimit
choosing the moment. Companion of make_config.py (terminus-2); see docs/other_harnesses.md.

    # static: the harness's own threshold, set at launch (no proxy)
    python configs/make_harness_config.py --harness claude-code --model anthropic/claude-sonnet-5-5 \
        --limit 87000 --tasks seismo_cross_correlation_lag__01 --hours 3
    python configs/make_harness_config.py --harness codex --model openai/gpt-6-luna --limit native ...
    # dynamic: optlimit proxy (balance = DTAP rule, needs only the compaction cost C)
    python configs/make_harness_config.py --harness codex --model openai/gpt-6-luna \
        --balance codex-luna-hub --prices gpt-6-luna --learn --tasks ... --hours 3

Run with scripts/run_harness_job.sh (starts the proxy when the config has a `# proxy:` line).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from make_config import _repo_relative, short_model  # noqa: E402

AGENTS = {
    "claude-code": "optlimit.adapters.harbor_installed:OptlimitClaudeCode",
    "codex": "optlimit.adapters.harbor_installed:OptlimitCodex",
}
# Proxy mode: the proxy reports min(P, low_cap) until the controller decides, then `high`.
# The harness's own trigger sits between them, so it fires on the signal only. limit_max keeps
# L* below the harness trigger and the API context limit (the true prompt is hidden from the harness).
BANDS = {
    # Claude Code: auto-compact window 200K (trigger ~ the window); signal far above it
    # Claude Code 2.1.3xx does not compact on the API-reported usage (smoke 2026-11-09), but it does run
    # its reactive compaction on a "prompt is too long" 400 -> trigger=error. It aborts the run when
    # the context refills within 3 turns of a compaction 3 times in a row ("Autocompact is
    # thrashing"), so a window must last >= 4 turns.
    "claude-code": {"native": {"window": 200_000}, "low_cap": 50_000, "high": 950_000, "limit_max": 180_000,
                    "trigger": "error", "min_window_turns": 4},
    # Codex: model_auto_compact_token_limit 200K under a 272K window; signal just under the window
    # (model_context_window is not set: it breaks Codex auto-compaction, openai/codex #16033)
    "codex": {"native": {"limit": 200_000}, "low_cap": 50_000, "high": 258_000,
              "limit_max": 180_000, "trigger": "usage", "min_window_turns": 3},
}


def _parse_tokens(v: str) -> int:
    v = v.lower().strip()
    mult = 1000 if v.endswith("k") else 1
    return int(float(v.rstrip("k")) * mult)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--harness", choices=sorted(AGENTS), required=True)
    ap.add_argument("--model", required=True, help="litellm-style id, e.g. anthropic/claude-sonnet-5-5, openai/gpt-6-luna")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--limit", help="static native threshold N tokens, or 'native' (harness default)")
    g.add_argument("--balance", metavar="PRIOR", help="optlimit balance (DTAP) mode via the proxy")
    g.add_argument("--formula", metavar="PRIOR", help="optlimit formula mode via the proxy")
    g.add_argument("--proxy-fixed", metavar="N",
                   help="fixed L enforced through the proxy (control: same L as --limit, dynamic trigger path)")
    ap.add_argument("--prices", help="configs/prices.json row (alpha, beta) for --balance/--formula")
    ap.add_argument("--learn", action="store_true",
                    help="learn Lp, summary output and gamma from this run's own compactions")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="extra controller field")
    ap.add_argument("--trigger", choices=["usage", "error"],
                    help="proxy trigger (default per harness: claude-code error, codex usage)")
    ap.add_argument("--observe", action="store_true",
                    help="static modes: still run the proxy with trigger=off to log every call's usage")
    ap.add_argument("--tasks-dir", default="./LHTB/tasks-2.0")
    ap.add_argument("--tasks", nargs="*", default=[])
    ap.add_argument("--env", choices=["docker", "daytona"], default="docker")
    ap.add_argument("--hours", type=float, default=3.0)
    ap.add_argument("--concurrent", type=int, default=1)
    ap.add_argument("--continue-mode", choices=["same_conversation", "phases"], default="same_conversation")
    ap.add_argument("--verifier-feedback", choices=["true", "false", "score"])
    ap.add_argument("--job-name")
    ap.add_argument("--jobs-dir", default="./jobs")
    ap.add_argument("--out")
    a = ap.parse_args()

    priors = json.loads((HERE / "priors.json").read_text())["harness"]
    prices = json.loads((HERE / "prices.json").read_text())["models"]
    band = BANDS[a.harness]
    trigger = a.trigger or band["trigger"]
    proxy: dict | None = None
    if a.limit is not None:
        if a.limit == "native":
            cond, compaction = "native", {}
        else:
            n = _parse_tokens(a.limit)
            cond, compaction = f"l{n // 1000}k", {"limit": n}
        if a.observe:
            proxy = {"trigger": "off", "spec": {"mode": "off"}}
    elif a.proxy_fixed is not None:
        n = _parse_tokens(a.proxy_fixed)
        cond = f"proxyl{n // 1000}k"
        compaction = dict(band["native"])
        spec = {"mode": "fixed", "fixed_limit": n, "lookahead": 0, "min_window_turns": band["min_window_turns"]}
        for kv in a.set:
            k, _, v = kv.partition("=")
            spec[k] = yaml.safe_load(v)
        proxy = {"trigger": trigger, "spec": spec, "low_cap": band["low_cap"], "high": band["high"],
                 "min_window_turns": spec["min_window_turns"]}
    else:
        prior = a.balance or a.formula
        if prior not in priors:
            ap.error(f"{prior!r} is not in configs/priors.json (known: {', '.join(sorted(priors))})")
        if a.prices and a.prices not in prices:
            ap.error(f"--prices {a.prices!r} is not in configs/prices.json (known: {', '.join(sorted(prices))})")
        mode = "balance" if a.balance else "formula"
        spec: dict = {"mode": mode, "prior": prior, "lookahead": 1 if mode == "formula" else 0}
        if a.prices:
            spec["prices"] = a.prices
        if a.learn:
            spec.update(learn_lp=True, learn_summary_out=True, learn_gamma=True)
        spec["limit_max"] = band["limit_max"]
        spec["min_window_turns"] = band["min_window_turns"]
        for kv in a.set:
            k, _, v = kv.partition("=")
            spec[k] = yaml.safe_load(v)
        cond = f"{mode}_{prior}"
        compaction = dict(band["native"])
        proxy = {"trigger": trigger, "spec": spec, "low_cap": band["low_cap"], "high": band["high"],
                 "min_window_turns": spec["min_window_turns"]}

    job = a.job_name or f"{a.harness}-{short_model(a.model)}_{cond}"
    out = Path(a.out) if a.out else HERE / "sweep" / f"{job}.yaml"
    agent = {"import_path": AGENTS[a.harness], "model_name": a.model,
             "override_timeout_sec": int(a.hours * 3600), "kwargs": {"compaction": compaction} if compaction else {}}
    env: dict = {"type": a.env, "force_build": False, "delete": True}
    if a.env == "daytona":
        env["kwargs"] = {"auto_stop_interval_mins": int(a.hours * 60) + 60, "auto_delete_interval_mins": 30}
    dataset: dict = {"path": a.tasks_dir}
    if a.tasks:
        dataset["task_names"] = a.tasks
    cfg = {"job_name": job, "jobs_dir": a.jobs_dir, "n_attempts": 1, "n_concurrent_trials": a.concurrent,
           "timeout_multiplier": 1.0, "environment": env, "agents": [agent], "datasets": [dataset]}
    env_lines = ["HB_CONTINUE_MODE=" + ("same_conversation" if a.continue_mode == "same_conversation" else "")]
    if a.verifier_feedback:
        env_lines.append(f"HB_VERIFIER_FEEDBACK={a.verifier_feedback}")
    header = (f"# generated by configs/make_harness_config.py: harness={a.harness} model={a.model} "
              f"condition={cond} env={a.env} budget={a.hours}h tasks={a.tasks or 'all'}\n"
              f"# env: {' '.join(env_lines)}\n"
              + (f"# proxy: {json.dumps(proxy, separators=(',', ':'))}\n" if proxy else "")
              + "# run: bash scripts/run_harness_job.sh " + _repo_relative(out) + "\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
