#!/usr/bin/env python3
"""Behavioral red flags per trial from terminus-2 trajectories.

    python analysis/trajectory_issues.py jobs/<job>... [--out FILE]

Per trial (main agent trajectory, copied context skipped):
  rejections        verifier rejections (binary "did not pass verification" turns)
  submits           mark_task_complete calls (terminus asks to confirm, so ~2 per submit)
  quick_resubmits   rejections followed by mark_task_complete within 3 agent turns
                    (resubmitting without real work)
  refusals          agent messages with refusal / give-up phrasing
  parse_errors      turns whose reply could not be parsed
  warnings          parser WARNINGS fed back to the agent
  tracebacks        Python tracebacks seen in terminal output
  truncated_output  terminal output cut by the output limit
  max_same_cmd      longest run of identical consecutive commands
  top_cmd           most repeated command (first 60 chars) and its count
  llm_errors        LLM call failures logged in trial.log
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

REJECTION = "did not pass verification"
REFUSAL = re.compile(
    r"\b(I can(?:'|no)t (?:help|assist|continue|complete)|I(?:'m| am) (?:unable|not able) to"
    r"|I (?:will|must) (?:stop|decline)|refus|give up|cannot be completed|not possible to complete)",
    re.I,
)
LLM_ERROR = re.compile(r"(Unknown Error in LLM interaction|Error in proactively summarizing"
                       r"|LiteLLM Retried|RateLimitError|insufficient_quota|InternalServerError)")

FIELDS = [
    "job", "task", "reward", "turns", "rejections", "submits", "quick_resubmits",
    "refusals", "parse_errors", "warnings", "tracebacks", "truncated_output",
    "max_same_cmd", "top_cmd", "top_cmd_count", "llm_errors", "refusal_examples",
]


def _reward(trial_dir: Path):
    try:
        data = json.loads((trial_dir / "result.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")


def analyze(trial_dir: Path, job: str) -> dict:
    steps = []
    agent_dir = trial_dir / "agent"
    files = [agent_dir / "trajectory.json"] + sorted(agent_dir.glob("trajectory.cont-*.json"))
    for f in files:
        if f.exists():
            steps += [s for s in json.loads(f.read_text())["steps"] if not s.get("is_copied_context")]

    row = dict.fromkeys(FIELDS, 0)
    row.update(job=job, task=trial_dir.name.rsplit("__", 1)[0], reward=_reward(trial_dir))
    cmds: list[str] = []
    since_rejection: int | None = None
    refusal_examples: list[str] = []
    for s in steps:
        msg = s.get("message") or ""
        obs = json.dumps(s.get("observation") or "", ensure_ascii=False)
        if s["source"] == "user":
            if REJECTION in msg:
                row["rejections"] += 1
                since_rejection = 0
            continue
        if s["source"] != "agent":
            continue
        row["turns"] += 1
        names = [t["function_name"] for t in s.get("tool_calls") or []]
        if "mark_task_complete" in names:
            row["submits"] += 1
            if since_rejection is not None and since_rejection < 3:
                row["quick_resubmits"] += 1
                since_rejection = None
        if since_rejection is not None:
            since_rejection += 1
        if REFUSAL.search(msg):
            row["refusals"] += 1
            if len(refusal_examples) < 2:
                m = REFUSAL.search(msg)
                refusal_examples.append(msg[max(0, m.start() - 60): m.end() + 80].replace("\n", " "))
        row["parse_errors"] += "parsing errors" in obs
        row["warnings"] += "WARNINGS" in obs
        row["tracebacks"] += obs.count("Traceback (most recent call last)")
        row["truncated_output"] += "output limited" in obs
        for t in s.get("tool_calls") or []:
            if t["function_name"] == "bash_command":
                cmds.append((t.get("arguments") or {}).get("keystrokes", "").strip())

    streak = best = 0
    for prev, cur in zip([None] + cmds, cmds):
        streak = streak + 1 if cur == prev else 1
        best = max(best, streak)
    row["max_same_cmd"] = best
    common = Counter(c for c in cmds if c).most_common(1)
    if common:
        row["top_cmd"], row["top_cmd_count"] = common[0][0][:60].replace("\n", "⏎"), common[0][1]
    log = trial_dir / "trial.log"
    if log.exists():
        row["llm_errors"] = len(LLM_ERROR.findall(log.read_text(errors="replace")))
    row["refusal_examples"] = " || ".join(refusal_examples)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_dirs", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    rows = []
    for job_dir in args.job_dirs:
        for trial_dir in sorted(job_dir.glob("*__*")):
            if (trial_dir / "agent" / "trajectory.json").exists():
                rows.append(analyze(trial_dir, job_dir.name))
    if not rows:
        print("no trajectories", file=sys.stderr)
        return 1

    cols = ["job", "task", "reward", "turns", "rejections", "submits", "quick_resubmits",
            "refusals", "parse_errors", "warnings", "tracebacks", "truncated_output",
            "max_same_cmd", "top_cmd_count", "llm_errors"]
    print("  ".join(cols))
    for r in rows:
        print("  ".join(str(r[c]) for c in cols))
    if args.out:
        with args.out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
