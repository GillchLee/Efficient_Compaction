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
from _common import (add_benchmark_args, benchmark_environment, benchmark_extras, continue_env_line, harbor_header_line,  # noqa: E402
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
    ap.add_argument("--no-reread-charge", action="store_true",
                    help="Re-read Charge off: do not add the cost of post-compaction re-reads to C "
                         "(on by default for formula/balance). Same as --set reread_charge=false")
    ap.add_argument("--productive-turn", action="store_true",
                    help="Productive-turn Charge: minimise the cost per turn that is not a re-read "
                         "(R = re-read calls per compaction x cost of a productive turn at the optimum). "
                         "Same as --set reread_productive=true")
    ap.add_argument("--reread-premium", action="store_true",
                    help="with --productive-turn: add (alpha - 1) x the tokens the re-read calls of a window bring back "
                         "to the compaction cost (Re-fetch premium). Same as --set reread_premium=true")
    ap.add_argument("--reorient", nargs="?", const="parallel", choices=["parallel", "plain"], default=None,
                    help="Parallel re-orientation: note behind the compaction summary asking the agent to fetch what "
                         "it needs in ONE response ('plain' = control wording without that instruction). "
                         "Codex (proxy) and in-process terminus-2. Same as --set reorient=MODE")
    ap.add_argument("--net-rent", action="store_true",
                    help="balance only, Net rent: leave the prompt growth of re-read calls out of the rent. "
                         "Same as --set rent_net_reread=true")
    ap.add_argument("--carry-files", nargs="?", const="8000", metavar="TOKENS",
                    help="File Carry-over: put earlier read output of the files the agent keeps re-reading behind each "
                         "compaction summary, within this token budget (default 8000 when given without a value; "
                         "off when absent). Same as --set carry_files=N")
    ap.add_argument("--carry-order", choices=["score", "reread_edited"], default=None,
                    help="File Carry-over: which files go into the block first. score (default): re-read score; "
                         "reread_edited: files the last window both re-read and edited first. "
                         "Same as --set carry_order=NAME")
    ap.add_argument("--carry-whole", nargs="?", const="64000", metavar="TOKENS",
                    help="Whole-file Carry-over (terminus-2; Codex on docker): a file re-read in consecutive windows is handed over "
                         "complete, read from disk at the compaction, within this token budget for all such files "
                         "(default 64000 when given without a value; off when absent). A file is never cut: one that "
                         "does not fit stays with --carry-files. Same as --set carry_whole=N")
    ap.add_argument("--carry-whole-after", type=int, default=None, metavar="N",
                    help="Whole-file Carry-over: consecutive windows that must re-read a file before it is carried "
                         "whole (default 2). Same as --set carry_whole_after=N")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="extra controller field")
    ap.add_argument("--attach-orphan-summaries", action="store_true",
                    help="proxy: charge compaction calls with a new message root (terminus-2 questions "
                         "subagent) to the compacting session (learned O_s/gamma)")
    ap.add_argument("--trigger", choices=["usage", "error"], help="proxy trigger (default: the harness profile's)")
    ap.add_argument("--observe", action="store_true",
                    help="static modes: still run the proxy with trigger=off to log every call's usage")
    add_benchmark_args(ap)
    ap.add_argument("--env", choices=["docker", "daytona", "singularity"], default="docker",
                    help="singularity: Apptainer/Singularity on a host without Docker (Harbor 0.25 profile; tasks need "
                         "[environment] docker_image, e.g. --benchmark swebench_verified_sif)")
    ap.add_argument("--sif-cache", metavar="DIR",
                    help="singularity: directory for the converted .sif images (default: $OPTLIMIT_SIF_CACHE at run "
                         "time, else ./data/sif-cache)")
    ap.add_argument("--concurrent", type=int, default=1)
    ap.add_argument("--attempts", type=int, default=1, help="n_attempts: runs of every task (repeats for a mean)")
    ap.add_argument("--artifact", action="append", default=[], metavar="PATH",
                    help="container path Harbor copies out after the agent phase, before the verifier "
                         "(repeatable), e.g. the workspace, so a trial can be graded again later")
    ap.add_argument("--artifact-exclude", action="append", default=[], metavar="PATTERN",
                    help="tar --exclude pattern applied to every --artifact directory (repeatable), e.g. target")
    ap.add_argument("--setup-timeout", type=int, metavar="SEC",
                    help="agent setup (CLI install in the container) timeout; default: the harness profile's, else Harbor's 360")
    ap.add_argument("--verifier-feedback", choices=["true", "false", "score"])
    ap.add_argument("--agent-version", metavar="X.Y.Z",
                    help="installed-agent CLI version to install in the task container (agent kwarg version; "
                         "default: the newest release at run time). Pin it to compare with an earlier run")
    ap.add_argument("--reasoning-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                    help="agent kwarg reasoning_effort (Codex: -c model_reasoning_effort); default: the CLI's own")
    ap.add_argument("--job-name")
    ap.add_argument("--jobs-dir", default="./jobs")
    ap.add_argument("--out")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.net_rent and a.balance is None:
        ap.error("--net-rent applies to --balance only")
    if a.no_reread_charge:
        a.set.append("reread_charge=false")
    if a.productive_turn:
        if a.no_reread_charge:
            ap.error("--productive-turn needs the Re-read Charge")
        a.set.append("reread_productive=true")
    if a.reread_premium:
        if not a.productive_turn:
            ap.error("--reread-premium needs --productive-turn")
        a.set.append("reread_premium=true")
    if a.reorient:
        a.set.append("reorient=" + a.reorient)
    if a.net_rent:
        a.set.append("rent_net_reread=true")
    if a.carry_files:
        a.set.append("carry_files=" + str(int(float(a.carry_files.lower().rstrip("k")) * (1000 if a.carry_files.lower().endswith("k") else 1))))
    if a.carry_whole:
        a.set.append("carry_whole=" + str(int(float(a.carry_whole.lower().rstrip("k")) * (1000 if a.carry_whole.lower().endswith("k") else 1))))
    if a.carry_whole_after is not None:
        if not a.carry_whole:
            ap.error("--carry-whole-after needs --carry-whole")
        a.set.append("carry_whole_after=" + str(a.carry_whole_after))
    if a.carry_order:
        if not a.carry_files:
            ap.error("--carry-order needs --carry-files")
        a.set.append("carry_order=" + a.carry_order)
    prof = harnesses.get(a.harness)
    priors = json.loads((HERE / "priors.json").read_text())["harness"]
    prices = json.loads((HERE / "prices.json").read_text())["models"]
    band = prof.band
    trigger = a.trigger or prof.trigger

    def proxy_block(spec: dict) -> dict:
        spec = {**spec, **parse_sets(a.set)}
        spec.setdefault("min_window_turns", prof.min_window_turns)
        blk = {"trigger": trigger, "spec": spec, "low_cap": band["low_cap"], "high": band["high"],
               "min_window_turns": spec["min_window_turns"]}
        if a.attach_orphan_summaries:
            blk["attach_orphan_summaries"] = True
        return blk

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
    if a.reasoning_effort:
        kwargs["reasoning_effort"] = a.reasoning_effort
    if getattr(a, "agent_version", None):
        kwargs["version"] = a.agent_version
    job = a.job_name or f"{a.harness}-{short_model(a.model)}_{cond}"
    out = Path(a.out) if a.out else HERE / "sweep" / f"{job}.yaml"
    agent = {**prof.agent, "model_name": a.model, "override_timeout_sec": int(hours * 3600), "kwargs": kwargs}
    setup_timeout = a.setup_timeout or prof.setup_timeout_sec
    if setup_timeout:
        agent["override_setup_timeout_sec"] = int(setup_timeout)
    if harbor != "lhtb-fork" and prof.install_hosts and a.env != "singularity":
        # Harbor >= 0.20 egress allowlists (singularity shares the host network: none): the CLI install has to get out of tasks that deny egress
        # (the proxy host itself is added at launch by scripts/run_harness_job.sh)
        agent["extra_allowed_hosts"] = list(prof.install_hosts)
    env: dict = {"type": a.env, "force_build": False, "delete": True}
    env.update(benchmark_environment(a))   # benchmarks.yaml `environment:` (kernelbench: delete: false)
    if a.env == "daytona":
        env["kwargs"] = {"auto_stop_interval_mins": int(hours * 60) + 60, "auto_delete_interval_mins": 30}
    if a.env == "singularity":
        if harbor == "lhtb-fork":
            ap.error("--env singularity needs the Harbor 0.25 profile (--harbor 0.25 or a 0.25 benchmark)")
        # singularity_no_mount: Harbor's default "home,tmp,bind-paths" also drops /etc/resolv.conf, so the
        # container has no DNS (agent install, uv/pip in the grader fail); keep the system bind paths
        env["kwargs"] = {"singularity_image_cache_dir": a.sif_cache or "./data/sif-cache",
                         "singularity_no_mount": "home,tmp"}
        env["delete"] = False          # nothing to delete; keeps the .sif cache
    elif harbor != "lhtb-fork" and prof.install_hosts:
        env["extra_allowed_hosts"] = list(prof.install_hosts)   # agent setup runs under the environment baseline
    cfg = {"job_name": job, "jobs_dir": a.jobs_dir, "n_attempts": a.attempts, "n_concurrent_trials": a.concurrent,
           "timeout_multiplier": 1.0, "environment": env, "agents": [agent], "datasets": [dataset]}
    if a.artifact:
        cfg["artifacts"] = [{"source": src, "exclude": list(a.artifact_exclude)} if a.artifact_exclude else src
                            for src in a.artifact]
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
