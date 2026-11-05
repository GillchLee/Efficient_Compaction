#!/usr/bin/env python3
"""Write a Harbor job config for one compaction condition.

    python configs/make_config.py --model anthropic/claude-sonnet-5-5 --limit 80000 \
        --tasks-dir ./LHTB/tasks-2.0 --tasks gdal_antimeridian_clip__01 sqlite_wal_recovery_audit__02 \
        --env docker --hours 3 [--job-name sonnet_l80k] [--out configs/sweep/sonnet_l80k.yaml]

Compaction condition (one of):
  --limit N          compact when the API-reported prompt reaches N tokens
                     (optlimit.harbor_agent, mode fixed; exact for every provider)
  --limit native     stock terminus-2 behaviour (mode off: only the 8000-free-token check)
  --formula PRIOR    optlimit chooses L* online from configs/priors.json[PRIOR] and
                     --prices ROW (configs/prices.json); any --set key=value overrides a field
  --native-threshold N
                     old style: stock terminus-2 with proactive_summarization_threshold=N
                     (litellm's local token count; for reproducing the earlier sweeps)

--handoff summary_words=2600 --handoff min_questions=12 --handoff answer_words=2000
                     length targets for the three summarization prompts (bigger Lp)
  --handoff mode=single --handoff summary_words=800
                     one structured summary call instead of terminus-2's three (m=1)
Environment: --env docker (default) or daytona; tasks from --tasks-dir, --tasks (names).

Local / self-hosted model (vLLM OpenAI-compatible server):
  --model hosted_vllm/<served-model-name> --api-base http://HOST:8000/v1 --context-window 131072
                     writes agent `api_base` and the `model_info` Harbor requires for hosted_vllm
                     (max_input_tokens = --context-window, max_output_tokens = --max-tokens,
                     costs 0 unless --model-info overrides); in formula mode also sets
                     optlimit limit_max = context - max_tokens - 8000 unless --set limit_max=...
  --prices vllm-...  a row of configs/prices.json in time units (scripts/measure_vllm_prices.py)

Trial-loop options (recorded as `# env:` lines in the yaml; scripts/run_job.sh exports them):
  --verifier-feedback false
                     HB_VERIFIER_FEEDBACK=false: the interim verifier still runs on every
                     submission (interim_verifier.jsonl) but the agent is resumed with a neutral
                     continuation instead of the binary rejection (patch 04). Default true.
  --continue-mode phases
                     HB_CONTINUE_MODE= (new conversation per phase, the LHTB leaderboard way)
                     instead of run_job.sh's default same_conversation.
Prints the output path; the file is a plain Harbor config and can be edited by hand.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def short_model(model: str) -> str:
    m = model.split("/")[-1]
    m = re.sub(r"^(claude|gpt)-", "", m)
    return re.sub(r"[^a-z0-9]+", "", m.lower())[:12]



def _repo_relative(path: Path) -> str:
    """Path as written in the yaml header: relative to the repo root when inside it."""
    root = Path(__file__).resolve().parents[1]
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--model", required=True, help="litellm model id, e.g. anthropic/claude-sonnet-5-5")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--limit", help="N tokens, or 'native'")
    g.add_argument("--formula", metavar="PRIOR", help="optlimit formula mode with this prior")
    g.add_argument("--balance", metavar="PRIOR", help="optlimit balance (DTAP) mode with this prior")
    g.add_argument("--native-threshold", type=int, metavar="N")
    ap.add_argument("--prices", help="configs/prices.json row (formula mode)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="extra optlimit controller field (e.g. lookahead=1, g_prior_weight=3)")
    ap.add_argument("--handoff", action="append", default=[], metavar="KEY=VALUE",
                    help="hand-off length targets (summary_words, min_questions, answer_words); "
                         "stock terminus-2 prompts when absent")
    ap.add_argument("--tasks-dir", default="./LHTB/tasks-2.0")
    ap.add_argument("--tasks", nargs="*", default=[], help="task directory names (default: every task)")
    ap.add_argument("--env", choices=["docker", "daytona"], default="docker")
    ap.add_argument("--hours", type=float, default=3.0, help="agent budget per task")
    ap.add_argument("--concurrent", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=16384)
    ap.add_argument("--retries", type=int, default=6, help="litellm num_retries per call")
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--api-base", help="OpenAI-compatible endpoint, e.g. http://host:8000/v1 (vLLM)")
    ap.add_argument("--context-window", type=int, metavar="N",
                    help="model context length; required for hosted_vllm/ (Harbor model_info)")
    ap.add_argument("--model-info", action="append", default=[], metavar="KEY=VALUE",
                    help="extra/override Harbor model_info fields (input_cost_per_token, ...)")
    ap.add_argument("--verifier-feedback", choices=["true", "false"],
                    help="HB_VERIFIER_FEEDBACK (default true: binary rejection after a failed submission)")
    ap.add_argument("--continue-mode", choices=["same_conversation", "phases"],
                    help="HB_CONTINUE_MODE (default same_conversation)")
    ap.add_argument("--job-name")
    ap.add_argument("--jobs-dir", default="./jobs")
    ap.add_argument("--out")
    a = ap.parse_args()

    if a.native_threshold is not None:
        cond, agent_kind = f"thr{a.native_threshold // 1000}k", "stock"
    elif a.formula:
        cond, agent_kind = f"formula_{a.formula}", "optlimit"
    elif a.balance:
        cond, agent_kind = f"balance_{a.balance}", "optlimit"
    elif a.limit == "native":
        cond, agent_kind = "native", "optlimit"
    else:
        n = int(float(a.limit.lower().rstrip("k")) * (1000 if a.limit.lower().endswith("k") else 1))
        a.limit = n
        cond, agent_kind = f"l{n // 1000}k", "optlimit"
    job = a.job_name or f"{short_model(a.model)}_{cond}"
    out = Path(a.out) if a.out else HERE / "sweep" / f"{job}.yaml"

    kwargs: dict = {
        "parser_name": "json",
        "enable_summarize": True,
        "proactive_summarization_threshold": a.native_threshold if a.native_threshold is not None else 8000,
        "record_terminal_session": True,
        "llm_call_kwargs": {"max_tokens": a.max_tokens, "num_retries": a.retries},
    }
    if a.temperature is not None:
        kwargs["temperature"] = a.temperature
    if agent_kind == "optlimit":
        if a.formula or a.balance:
            prior = a.formula or a.balance
            priors = json.loads((HERE / "priors.json").read_text()).get("harness", {})
            if prior not in priors:
                ap.error(f"--formula/--balance {prior!r} is not in configs/priors.json "
                         f"(known: {', '.join(sorted(priors))})")
            ol = {"mode": "formula" if a.formula else "balance", "prior": prior, "lookahead": 1}
            if a.prices:
                prices = json.loads((HERE / "prices.json").read_text()).get("models", {})
                if a.prices not in prices:
                    ap.error(f"--prices {a.prices!r} is not in configs/prices.json "
                             f"(known: {', '.join(sorted(prices))}); run scripts/measure_vllm_prices.py first")
                ol["prices"] = a.prices
        elif a.limit == "native":
            ol = {"mode": "off"}
        else:
            ol = {"mode": "fixed", "fixed_limit": a.limit, "lookahead": 0}
        for kv in a.set:
            k, _, v = kv.partition("=")
            try:
                ol[k] = yaml.safe_load(v)
            except yaml.YAMLError:
                ol[k] = v
        kwargs["optlimit"] = ol
        if a.handoff:
            kwargs["handoff"] = {}
            for kv in a.handoff:
                k, _, v = kv.partition("=")
                kwargs["handoff"][k] = int(v) if v.isdigit() else v
        agent = {"import_path": "optlimit.harbor_agent:OptlimitTerminus2"}
    else:
        agent = {"name": "terminus-2"}
    if a.api_base:
        kwargs["api_base"] = a.api_base
    if a.model.startswith("hosted_vllm/") and a.context_window is None and not a.model_info:
        ap.error("hosted_vllm/ models need --context-window N (Harbor requires model_info)")
    if a.context_window is not None or a.model_info:
        info: dict = {}
        if a.context_window is not None:
            info = {"max_input_tokens": a.context_window, "max_output_tokens": a.max_tokens,
                    "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
        for kv in a.model_info:
            k, _, v = kv.partition("=")
            info[k] = yaml.safe_load(v)
        kwargs["model_info"] = info
        ol = kwargs.get("optlimit")
        if a.context_window is not None and ol and ol.get("mode") in ("formula", "balance") \
                and "limit_max" not in ol:
            # keep L* below the point where terminus-2's own free-token check (8000) fires
            ol["limit_max"] = max(0, a.context_window - a.max_tokens - 8000)
        if a.context_window is not None and isinstance(a.limit, int) \
                and a.limit > a.context_window - a.max_tokens - 8000:
            print(f"warning: --limit {a.limit} is above context - max_tokens - 8000 = "
                  f"{a.context_window - a.max_tokens - 8000}; terminus-2 compacts earlier", file=sys.stderr)
    agent.update({"model_name": a.model, "override_timeout_sec": int(a.hours * 3600), "kwargs": kwargs})
    env_lines = []
    if a.verifier_feedback:
        env_lines.append(f"HB_VERIFIER_FEEDBACK={a.verifier_feedback}")
    if a.continue_mode:
        env_lines.append("HB_CONTINUE_MODE=" + ("same_conversation" if a.continue_mode == "same_conversation" else ""))

    env: dict = {"type": a.env, "force_build": False, "delete": True}
    if a.env == "daytona":
        env["kwargs"] = {"auto_stop_interval_mins": int(a.hours * 60) + 60, "auto_delete_interval_mins": 30}

    dataset: dict = {"path": a.tasks_dir}
    if a.tasks:
        dataset["task_names"] = a.tasks
    cfg = {
        "job_name": job, "jobs_dir": a.jobs_dir, "n_attempts": 1,
        "n_concurrent_trials": a.concurrent, "timeout_multiplier": 1.0,
        "environment": env, "agents": [agent], "datasets": [dataset],
    }
    header = (f"# generated by configs/make_config.py: model={a.model} condition={cond} env={a.env} "
              f"budget={a.hours}h tasks={a.tasks or 'all'}"
              + (f" handoff={','.join(a.handoff)}" if a.handoff else "")
              + (f" api_base={a.api_base}" if a.api_base else "") + "\n"
              + (f"# env: {' '.join(env_lines)}\n" if env_lines else "")
              + "# run: bash scripts/run_job.sh " + _repo_relative(out) + "\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
