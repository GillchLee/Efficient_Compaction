#!/usr/bin/env python3
"""Per-turn prompt growth and prompt-cache usage from Harbor terminus-2 trajectories.

    python scripts/token_turns.py jobs/<job_name> [--out-dir DIR]

Writes, under DIR (default: <job_dir>/token_turns/):
  turns.csv    one row per LLM call (main agent + summarization subagents)
  summary.csv  one row per trial

Columns in turns.csv:
  prompt_tokens     total input tokens of the call (uncached + cache_read + cache_write)
  prompt_delta      prompt_tokens minus the previous main-agent call's prompt_tokens
  cache_read        cache hits (Anthropic cache_read_input_tokens)
  cache_write       tokens written to the cache (cache_creation_input_tokens)
  uncached          prompt_tokens - cache_read - cache_write
  completion_tokens output tokens of the call (for OpenAI this includes reasoning)
  phase             0 for the first run, +1 after every verifier rejection
                    (same-conversation resume) — read from the user steps
  kind              main, or summarization for the summary/questions/answers/short
                    LLM calls made while compacting (see the file column)

summary.csv adds, per trial:
  compactions       context summarizations (handoffs) in the main agent
  main_* / summ_*   tokens and cost split between agent turns and compaction calls
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

TURN_FIELDS = [
    "trial",
    "task",
    "file",
    "kind",
    "phase",
    "turn",
    "step_id",
    "timestamp",
    "prompt_tokens",
    "prompt_delta",
    "cache_read",
    "cache_write",
    "uncached",
    "completion_tokens",
    "cost_usd",
]

SUMMARY_FIELDS = [
    "trial",
    "task",
    "reward",
    "phases",
    "compactions",
    "turns",
    "max_prompt_tokens",
    "final_prompt_tokens",
    "sum_prompt_tokens",
    "sum_cache_read",
    "sum_cache_write",
    "sum_uncached",
    "sum_completion_tokens",
    "cache_hit_ratio",
    "cost_usd",
    "main_prompt_tokens",
    "main_completion_tokens",
    "main_cost_usd",
    "summ_calls",
    "summ_prompt_tokens",
    "summ_cache_read",
    "summ_cache_write",
    "summ_completion_tokens",
    "summ_cost_usd",
]

# Both binary-feedback rejection prompts (harbor/src/harbor/trial/trial.py) say this.
REJECTION_MARKER = "did not pass verification"


def _cont_index(path: Path) -> int:
    m = re.fullmatch(r"trajectory\.cont-(\d+)\.json", path.name)
    return int(m.group(1)) if m else 0


# Call order within one compaction (terminus_2._summarize, then its short fallback).
_SUBAGENT_ORDER = {"summary": 0, "questions": 1, "answers": 2, "short": 3}


def _summarization_key(path: Path) -> tuple[int, int, str]:
    m = re.fullmatch(r"trajectory\.summarization-(\d+)-(.+)\.json", path.name)
    if not m:
        return (0, 0, path.name)
    return (int(m.group(1)), _SUBAGENT_ORDER.get(m.group(2), 9), m.group(2))


def _main_trajectories(agent_dir: Path) -> list[Path]:
    files = [agent_dir / "trajectory.json"] if (agent_dir / "trajectory.json").exists() else []
    files += sorted(agent_dir.glob("trajectory.cont-*.json"), key=_cont_index)
    return files


def _reward(trial_dir: Path):
    result = trial_dir / "result.json"
    if not result.exists():
        return None
    try:
        data = json.loads(result.read_text())
    except json.JSONDecodeError:
        return None
    rewards = (data.get("verifier_result") or {}).get("rewards") or {}
    return rewards.get("reward")


def _rows_from_file(path: Path, trial: str, task: str, kind: str, state: dict) -> list[dict]:
    data = json.loads(path.read_text())
    rows = []
    for step in data.get("steps", []):
        if step.get("is_copied_context"):
            continue
        if kind == "main" and step.get("source") == "user":
            # Instruction, summarization handoffs and verifier rejections are all user
            # steps; only rejections start a new phase.
            if REJECTION_MARKER in (step.get("message") or ""):
                state["phase"] = state.get("phase", 0) + 1
            continue
        if step.get("source") == "system":
            if kind == "main":
                state["compactions"] = state.get("compactions", 0) + 1
            continue
        metrics = step.get("metrics")
        if step.get("source") != "agent" or not metrics:
            continue

        prompt = metrics.get("prompt_tokens") or 0
        cache_read = metrics.get("cached_tokens") or 0
        cache_write = (metrics.get("extra") or {}).get("cache_creation_tokens") or 0
        row = {
            "trial": trial,
            "task": task,
            "file": path.name,
            "kind": kind,
            "phase": state.get("phase", 0) if kind == "main" else "",
            "turn": "",
            "step_id": step.get("step_id"),
            "timestamp": step.get("timestamp"),
            "prompt_tokens": prompt,
            "prompt_delta": "",
            "cache_read": cache_read,
            "cache_write": cache_write,
            "uncached": max(prompt - cache_read - cache_write, 0),
            "completion_tokens": metrics.get("completion_tokens") or 0,
            "cost_usd": metrics.get("cost_usd") or 0,
        }
        if kind == "main":
            state["turn"] = state.get("turn", 0) + 1
            row["turn"] = state["turn"]
            prev = state.get("prev_prompt")
            row["prompt_delta"] = prompt - prev if prev is not None else prompt
            state["prev_prompt"] = prompt
        rows.append(row)
    return rows


def collect(job_dir: Path) -> tuple[list[dict], list[dict]]:
    turns: list[dict] = []
    summaries: list[dict] = []
    for agent_dir in sorted(job_dir.glob("*/agent")):
        trial_dir = agent_dir.parent
        trial = trial_dir.name
        task = trial.rsplit("__", 1)[0]
        state: dict = {}
        trial_rows: list[dict] = []
        for path in _main_trajectories(agent_dir):
            trial_rows += _rows_from_file(path, trial, task, "main", state)
        for path in sorted(
            agent_dir.glob("trajectory.summarization-*.json"), key=_summarization_key
        ):
            trial_rows += _rows_from_file(path, trial, task, "summarization", {})
        if not trial_rows:
            continue
        turns += trial_rows

        main = [r for r in trial_rows if r["kind"] == "main"]
        summ = [r for r in trial_rows if r["kind"] == "summarization"]
        sum_prompt = sum(r["prompt_tokens"] for r in trial_rows)
        sum_read = sum(r["cache_read"] for r in trial_rows)
        summaries.append(
            {
                "trial": trial,
                "task": task,
                "reward": _reward(trial_dir),
                "phases": state.get("phase", 0) + 1,
                "compactions": state.get("compactions", 0),
                "turns": len(main),
                "max_prompt_tokens": max((r["prompt_tokens"] for r in main), default=0),
                "final_prompt_tokens": main[-1]["prompt_tokens"] if main else 0,
                "sum_prompt_tokens": sum_prompt,
                "sum_cache_read": sum_read,
                "sum_cache_write": sum(r["cache_write"] for r in trial_rows),
                "sum_uncached": sum(r["uncached"] for r in trial_rows),
                "sum_completion_tokens": sum(r["completion_tokens"] for r in trial_rows),
                "cache_hit_ratio": round(sum_read / sum_prompt, 4) if sum_prompt else 0,
                "cost_usd": round(sum(r["cost_usd"] for r in trial_rows), 4),
                "main_prompt_tokens": sum(r["prompt_tokens"] for r in main),
                "main_completion_tokens": sum(r["completion_tokens"] for r in main),
                "main_cost_usd": round(sum(r["cost_usd"] for r in main), 4),
                "summ_calls": len(summ),
                "summ_prompt_tokens": sum(r["prompt_tokens"] for r in summ),
                "summ_cache_read": sum(r["cache_read"] for r in summ),
                "summ_cache_write": sum(r["cache_write"] for r in summ),
                "summ_completion_tokens": sum(r["completion_tokens"] for r in summ),
                "summ_cost_usd": round(sum(r["cost_usd"] for r in summ), 4),
            }
        )
    return turns, summaries


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()

    if not args.job_dir.is_dir():
        print(f"not a directory: {args.job_dir}", file=sys.stderr)
        return 1
    out_dir = args.out_dir or args.job_dir / "token_turns"
    out_dir.mkdir(parents=True, exist_ok=True)

    turns, summaries = collect(args.job_dir)
    _write(out_dir / "turns.csv", TURN_FIELDS, turns)
    _write(out_dir / "summary.csv", SUMMARY_FIELDS, summaries)

    print(f"{len(turns)} LLM calls across {len(summaries)} trials -> {out_dir}")
    for s in summaries:
        print(
            f"  {s['task']:<36} reward={s['reward']} phases={s['phases']} "
            f"turns={s['turns']} max_prompt={s['max_prompt_tokens']:,} "
            f"cache_read={s['sum_cache_read']:,} cache_write={s['sum_cache_write']:,} "
            f"output={s['sum_completion_tokens']:,} hit={s['cache_hit_ratio']:.1%} "
            f"${s['cost_usd']} (summ {s['summ_calls']} calls ${s['summ_cost_usd']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
