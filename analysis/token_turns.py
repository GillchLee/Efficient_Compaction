#!/usr/bin/env python3
"""Per-turn prompt growth, prompt-cache usage and submissions from Harbor terminus-2 trajectories.

    python analysis/token_turns.py jobs/<job_name> [--out-dir DIR] [--usage LOG.jsonl]

Writes, under DIR (default: <job_dir>/token_turns/):
  turns.csv        one row per LLM call (main agent + summarization subagents)
  summary.csv      one row per trial
  submissions.csv  one row per submission (interim verifier run)

Columns in turns.csv:
  prompt_tokens     total input tokens of the call (uncached + cache_read + cache_write)
  prompt_delta      prompt_tokens minus the previous main-agent call's prompt_tokens
  cache_read        cache hits (Anthropic cache_read_input_tokens / OpenAI cached_tokens)
  cache_write       tokens written to the cache (Anthropic cache_creation_input_tokens);
                    recorded by the Harbor patch, or taken from --usage for older runs
  uncached          prompt_tokens - cache_read - cache_write
  completion_tokens output tokens of the call (for OpenAI this includes reasoning)
  phase             0 for the first run, +1 after every verifier rejection
                    (same-conversation resume) - read from the user steps
  kind              main, or summarization for the summary/questions/answers/short
                    LLM calls made while compacting (see the file column)
  compaction        1 on the first main turn after a context summarization (the prompt
                    of this turn is the compacted context L_p)
  rejected          1 on the first main turn after a verifier rejection
  has_cmd           1 when the turn issued at least one terminal command
  ol_limit, ol_g_hat, ol_compact
                    optlimit's L*, growth estimate and decision taken from this turn's
                    prompt, when the trial ran optlimit.harbor_agent
                    (agent/optlimit_decisions.jsonl)

submissions.csv: from <trial>/interim_verifier.jsonl (Harbor patch) or the older
  phase_rewards[_sampled].jsonl: submission number, turn at which the agent submitted,
  reward, status (ok / timeout), passed, remaining_sec.

summary.csv adds, per trial: model, condition (from the job config / name), reward,
  end (timeout / passed / ended / error:X), phases, compactions, turns, cmd_turns,
  duration_min, first_completion_min, token totals split between agent turns (main_*)
  and compaction calls (summ_*), submissions and best / last interim reward.

--usage: per-call usage log (jsonl with ts, prompt_tokens, cache_write, conv_key) written
  by the litellm callback of the earlier project; records are matched to steps by time
  and prompt size and only fill cache_write.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TURN_FIELDS = [
    "trial", "task", "file", "kind", "phase", "turn", "step_id", "timestamp",
    "prompt_tokens", "prompt_delta", "cache_read", "cache_write", "uncached",
    "completion_tokens", "cost_usd", "compaction", "rejected", "has_cmd",
    "ol_limit", "ol_g_hat", "ol_compact",
]

SUMMARY_FIELDS = [
    "trial", "task", "model", "condition", "reward", "end", "phases", "compactions", "turns",
    "cmd_turns", "duration_min", "first_completion_min", "max_prompt_tokens",
    "final_prompt_tokens", "sum_prompt_tokens", "sum_cache_read", "sum_cache_write",
    "sum_uncached", "sum_completion_tokens", "cache_hit_ratio", "cost_usd",
    "main_prompt_tokens", "main_completion_tokens", "main_cost_usd", "summ_calls",
    "summ_prompt_tokens", "summ_cache_read", "summ_cache_write", "summ_completion_tokens",
    "summ_cost_usd", "submissions", "best_interim_reward", "last_interim_reward",
]

SUBMISSION_FIELDS = [
    "trial", "task", "submission", "timestamp", "turn", "reward", "status", "passed",
    "remaining_sec",
]

# Both binary-feedback rejection prompts (harbor/src/harbor/trial/trial.py) start like this.
REJECTION_PREFIXES = (
    "Your submitted solution did not pass verification",
    "Your previous attempt did not pass verification",
)
SUMMARY_MARKER = "context summarization"


def _text(m) -> str:
    if isinstance(m, str):
        return m
    if isinstance(m, list):
        return "".join(p.get("text", "") for p in m if isinstance(p, dict))
    return ""


def _ts(s):
    """ISO timestamp (or epoch number) -> epoch seconds, None when missing."""
    if s in (None, ""):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


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


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _result(trial_dir: Path) -> dict:
    """reward, end, phases, first completion from result.json."""
    data = _json(trial_dir / "result.json") or {}
    reward = ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    meta = (data.get("agent_result") or {}).get("metadata") or {}
    ex = (data.get("exception_info") or {}).get("exception_type")
    if ex == "AgentTimeoutError":
        end = "timeout"
    elif ex:
        end = f"error:{ex}"
    elif reward is not None and reward >= 1:
        end = "passed"
    elif data:
        end = "ended"
    else:
        end = ""
    fce = meta.get("first_completion_elapsed_sec")
    return {
        "reward": reward, "end": end,
        "phases": meta.get("continue_until_timeout_phases"),
        "first_completion_min": round(fce / 60, 2) if fce else "",
    }


def job_condition(job_dir: Path) -> tuple[str, str]:
    """(model id, condition label) from the job's config.json and name.

    A window token in the job name (_l80k, -ctx80k, _native; an optional task suffix such
    as _l80k_gdal may follow) wins, since older sweeps encoded the window there; otherwise
    the optlimit block (fixed -> l<N>k, off -> native, formula -> formula:<prior>) or the
    stock threshold (native, thr<N>k)."""
    cfg = _json(job_dir / "config.json") or {}
    agents = cfg.get("agents") or ([cfg["agent"]] if cfg.get("agent") else [])
    a = agents[0] if agents else {}
    model = (a.get("model_name") or "").split("/")[-1]
    kw = a.get("kwargs") or {}
    m = re.search(r"[_-](?:ctx)?(l?\d+k|native)(?:[_-][a-z0-9]+)*$", job_dir.name)
    if m:
        c = m.group(1)
        cond = c if c == "native" or c.startswith("l") else "l" + c
    elif "optlimit" in kw:
        ol = kw["optlimit"] or {}
        mode = ol.get("mode", "formula")
        if mode == "fixed":
            cond = f"l{int(ol.get('fixed_limit', 0)) // 1000}k"
        elif mode == "off":
            cond = "native"
        else:
            cond = "formula:" + str(ol.get("prior", "?"))
    else:
        thr = kw.get("proactive_summarization_threshold", 8000) or 0
        cond = "native" if thr <= 8000 else f"thr{thr // 1000}k"
    return model, cond


def _rows_from_file(path: Path, trial: str, task: str, kind: str, state: dict) -> list[dict]:
    data = _json(path) or {}
    rows: list[dict] = []
    for step in data.get("steps", []):
        if step.get("is_copied_context"):
            continue
        src = step.get("source")
        if kind == "main" and src == "user":
            msg = _text(step.get("message")).lstrip()
            if state.get("first_user") is None:
                state["first_user"] = msg
            # Instruction, summarization handoffs and verifier rejections are all user
            # steps; only rejections start a new phase.
            if msg.startswith(REJECTION_PREFIXES):
                state["phase"] = state.get("phase", 0) + 1
                state["rejected"] = 1
            continue
        if src == "system":
            if kind == "main" and SUMMARY_MARKER in _text(step.get("message")).lower():
                state["compactions"] = state.get("compactions", 0) + 1
                state["compaction"] = 1
            continue
        metrics = step.get("metrics")
        if src != "agent" or not metrics:
            continue

        prompt = metrics.get("prompt_tokens") or 0
        cache_read = metrics.get("cached_tokens") or 0
        cache_write = (metrics.get("extra") or {}).get("cache_creation_tokens")
        row = {
            "trial": trial, "task": task, "file": path.name, "kind": kind,
            "phase": state.get("phase", 0) if kind == "main" else "",
            "turn": "", "step_id": step.get("step_id"), "timestamp": step.get("timestamp"),
            "prompt_tokens": prompt, "prompt_delta": "", "cache_read": cache_read,
            "cache_write": cache_write if cache_write is not None else "",
            "uncached": max(prompt - cache_read - (cache_write or 0), 0),
            "completion_tokens": metrics.get("completion_tokens") or 0,
            "cost_usd": metrics.get("cost_usd") or 0,
            "compaction": "", "rejected": "", "has_cmd": "",
            "ol_limit": "", "ol_g_hat": "", "ol_compact": "",
        }
        if kind == "main":
            state["turn"] = state.get("turn", 0) + 1
            row["turn"] = state["turn"]
            prev = state.get("prev_prompt")
            row["prompt_delta"] = prompt - prev if prev is not None else prompt
            state["prev_prompt"] = prompt
            row["compaction"] = state.pop("compaction", 0)
            row["rejected"] = state.pop("rejected", 0)
            row["has_cmd"] = int(any((c.get("arguments") or {}).get("keystrokes")
                                     for c in (step.get("tool_calls") or [])))
        rows.append(row)
    return rows


def _attach_decisions(agent_dir: Path, main_rows: list[dict]) -> None:
    """optlimit_decisions.jsonl (optlimit.harbor_agent): decision k was taken from the
    prompt of main turn k, right before turn k+1."""
    path = agent_dir / "optlimit_decisions.jsonl"
    if not path.exists():
        return
    by_turn = {r["turn"]: r for r in main_rows}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = by_turn.get(d.get("turn_idx"))
        if r is None:
            continue
        r["ol_limit"] = round(d["limit"]) if d.get("limit") is not None else ""
        r["ol_g_hat"] = round(d["g_hat"]) if d.get("g_hat") is not None else ""
        r["ol_compact"] = int(bool(d.get("compact_now")))


def _attach_usage(rows: list[dict], usage_path: Path, first_user: str) -> None:
    """Fill cache_write from a per-call usage log (older runs without the Harbor patch).

    A record belongs to the first step that finished after it; inside that window the
    record with the same prompt_tokens is the step's call. Records of other conversations
    (other trials of the same job) are told apart by conv_key."""
    key = hashlib.sha1(first_user[:4000].encode("utf-8", "ignore")).hexdigest()[:12]
    recs = []
    for line in usage_path.read_text().splitlines():
        if line.strip():
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    recs.sort(key=lambda r: r.get("ts", 0))
    stamped = [(_ts(r["timestamp"]), r) for r in rows if _ts(r["timestamp"]) is not None]
    stamped.sort(key=lambda x: x[0])
    if not stamped:
        return
    t0 = stamped[0][0] - 3600
    recs = [r for r in recs if r.get("ts", 0) >= t0]
    j = 0
    for te, row in stamped:
        window = []
        while j < len(recs) and recs[j]["ts"] <= te + 1.0:
            window.append(recs[j])
            j += 1
        match = next((r for r in reversed(window) if r.get("prompt_tokens") == row["prompt_tokens"]), None)
        if match is None and row["kind"] == "main":
            match = next((r for r in reversed(window) if r.get("conv_key") == key), None)
        if match is not None:
            row["cache_write"] = match.get("cache_write") or 0
            row["uncached"] = max(row["prompt_tokens"] - row["cache_read"] - row["cache_write"], 0)


def _submissions(trial_dir: Path, trial: str, task: str, main_rows: list[dict]) -> list[dict]:
    times = [(_ts(r["timestamp"]), r["turn"]) for r in main_rows if _ts(r["timestamp"]) is not None]
    rows: list[dict] = []
    for name in ("interim_verifier.jsonl", "phase_rewards.jsonl", "phase_rewards_sampled.jsonl"):
        log = trial_dir / name
        if not log.exists():
            continue
        for line in log.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = _ts(rec.get("timestamp", rec.get("ts")))
            try:
                reward = float(rec["reward"]) if rec.get("reward") not in (None, "") else None
            except (TypeError, ValueError):
                reward = None
            status = rec.get("status") or ("ok" if reward is not None else "timeout")
            passed = rec.get("passed")
            if passed is None:
                passed = reward is not None and reward >= 1
            rows.append({
                "trial": trial, "task": task,
                "submission": rec.get("submission", rec.get("phase")),
                "timestamp": rec.get("timestamp") or (
                    datetime.fromtimestamp(at, timezone.utc).isoformat() if at else ""),
                # Agent turns completed before this verifier run finished.
                "turn": max((turn for t, turn in times if t <= at), default=0) if at else "",
                "reward": reward, "status": status, "passed": passed,
                "remaining_sec": rec.get("remaining_sec"),
            })
    rows.sort(key=lambda r: (r["timestamp"], r["submission"] or 0))
    return rows


def collect(job_dir: Path, usage: Path | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    model, condition = job_condition(job_dir)
    turns: list[dict] = []
    summaries: list[dict] = []
    submissions: list[dict] = []
    for agent_dir in sorted(job_dir.glob("*/agent")):
        trial_dir = agent_dir.parent
        trial = trial_dir.name
        task = trial.rsplit("__", 1)[0]
        state: dict = {}
        trial_rows: list[dict] = []
        for path in _main_trajectories(agent_dir):
            trial_rows += _rows_from_file(path, trial, task, "main", state)
        for path in sorted(agent_dir.glob("trajectory.summarization-*.json"), key=_summarization_key):
            trial_rows += _rows_from_file(path, trial, task, "summarization", {})
        if not trial_rows:
            continue
        main = [r for r in trial_rows if r["kind"] == "main"]
        summ = [r for r in trial_rows if r["kind"] == "summarization"]
        _attach_decisions(agent_dir, main)
        if usage is not None and usage.exists():
            _attach_usage(trial_rows, usage, state.get("first_user") or "")
        turns += trial_rows

        subs = _submissions(trial_dir, trial, task, main)
        submissions += subs
        sub_rewards = [r["reward"] for r in subs if r["reward"] is not None]
        sum_prompt = sum(r["prompt_tokens"] for r in trial_rows)
        sum_read = sum(r["cache_read"] for r in trial_rows)
        stamps = [_ts(r["timestamp"]) for r in main if _ts(r["timestamp"]) is not None]
        res = _result(trial_dir)
        summaries.append({
            "trial": trial, "task": task, "model": model, "condition": condition,
            "reward": res["reward"], "end": res["end"],
            "phases": res["phases"] if res["phases"] is not None else state.get("phase", 0) + 1,
            "compactions": state.get("compactions", 0), "turns": len(main),
            "cmd_turns": sum(r["has_cmd"] or 0 for r in main),
            "duration_min": round((max(stamps) - min(stamps)) / 60, 1) if len(stamps) > 1 else "",
            "first_completion_min": res["first_completion_min"],
            "max_prompt_tokens": max((r["prompt_tokens"] for r in main), default=0),
            "final_prompt_tokens": main[-1]["prompt_tokens"] if main else 0,
            "sum_prompt_tokens": sum_prompt, "sum_cache_read": sum_read,
            "sum_cache_write": sum(r["cache_write"] or 0 for r in trial_rows),
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
            "summ_cache_write": sum(r["cache_write"] or 0 for r in summ),
            "summ_completion_tokens": sum(r["completion_tokens"] for r in summ),
            "summ_cost_usd": round(sum(r["cost_usd"] for r in summ), 4),
            "submissions": len(subs),
            "best_interim_reward": max(sub_rewards, default=None),
            "last_interim_reward": sub_rewards[-1] if sub_rewards else None,
        })
    return turns, summaries, submissions


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--usage", type=Path, help="per-call usage log of an older run (fills cache_write)")
    args = parser.parse_args()

    if not args.job_dir.is_dir():
        print(f"not a directory: {args.job_dir}", file=sys.stderr)
        return 1
    out_dir = args.out_dir or args.job_dir / "token_turns"
    out_dir.mkdir(parents=True, exist_ok=True)

    turns, summaries, submissions = collect(args.job_dir, args.usage)
    _write(out_dir / "turns.csv", TURN_FIELDS, turns)
    _write(out_dir / "summary.csv", SUMMARY_FIELDS, summaries)
    _write(out_dir / "submissions.csv", SUBMISSION_FIELDS, submissions)

    print(f"{len(turns)} LLM calls across {len(summaries)} trials -> {out_dir}")
    for s in summaries:
        print(
            f"  {s['task']:<36} reward={s['reward']} end={s['end']} phases={s['phases']} "
            f"turns={s['turns']} compactions={s['compactions']} max_prompt={s['max_prompt_tokens']:,} "
            f"cache_read={s['sum_cache_read']:,} cache_write={s['sum_cache_write']:,} "
            f"output={s['sum_completion_tokens']:,} hit={s['cache_hit_ratio']:.1%} "
            f"${s['cost_usd']} (summ {s['summ_calls']} calls ${s['summ_cost_usd']}) "
            f"submissions={s['submissions']} best={s['best_interim_reward']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
