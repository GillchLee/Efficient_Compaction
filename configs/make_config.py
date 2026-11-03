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
Prints the output path; the file is a plain Harbor config and can be edited by hand.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def short_model(model: str) -> str:
    m = model.split("/")[-1]
    m = re.sub(r"^(claude|gpt)-", "", m)
    return re.sub(r"[^a-z0-9]+", "", m.lower())[:12]


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
            ol = {"mode": "formula" if a.formula else "balance", "prior": a.formula or a.balance, "lookahead": 1}
            if a.prices:
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
    agent.update({"model_name": a.model, "override_timeout_sec": int(a.hours * 3600), "kwargs": kwargs})

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
              + (f" handoff={','.join(a.handoff)}" if a.handoff else "") + "\n"
              "# run: bash scripts/run_job.sh " + str(out) + "\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
