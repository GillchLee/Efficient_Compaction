#!/usr/bin/env python3
"""Per-call and per-session tables from an optlimit proxy log (jobs/logs/<job>.proxy.jsonl).

    python analysis/proxy_turns.py jobs/logs/<job>.proxy.jsonl [--prices ROW | --prices-from SPEC.json] [--out-dir DIR]

For harnesses that compact internally (Claude Code, Codex) the proxy log is the token record:
true prompt / cache read / cache write / output of every call (main turns and the harness's own
compaction calls), the controller's decision, and what the harness was shown (reported_prompt).

Writes <log stem>.calls.csv and .sessions.csv (default: next to the log). With --job-dir it also
writes <job-dir>/token_turns/{turns,summary}.csv in analysis/token_turns.py's format (one "trial"
per proxy session), so analysis/replay.py, prefill_stats.py and fit_priors.py run unchanged:

    python analysis/proxy_turns.py jobs/logs/<job>.proxy.jsonl --job-dir jobs/<job>-proxy --condition native
    python analysis/replay.py jobs/<job>-proxy --prior codex-luna-hub --prices gpt-6-luna --limits 40000 80000
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optlimit.controller import load_prices  # noqa: E402

CALL_FIELDS = ["session", "i", "ts", "kind", "action", "status", "prompt", "cached", "cache_write", "uncached",
               "output", "reasoning", "cost_usd", "window_idx", "turn_idx", "window_start", "compact_now",
               "limit", "rent", "C", "reported_prompt", "reason"]
SESSION_FIELDS = ["session", "main_calls", "summary_calls", "rejected", "compactions", "compact_signals",
                  "max_prompt", "mean_prompt", "cache_hit", "cost_usd", "main_cost_usd", "summary_cost_usd",
                  "summary_prompt", "summary_output", "root_preview"]


def _num(v) -> float:
    return float(v or 0)


def write_token_turns(calls: list[dict], sessions: list[dict], job_dir: Path, condition: str, model: str) -> None:
    """analysis/token_turns.py-format tables from proxy calls (trial = proxy session)."""
    tt = job_dir / "token_turns"
    tt.mkdir(parents=True, exist_ok=True)
    turns, prev, n = [], {}, {}
    for c in calls:
        if c["prompt"] is None or c["action"] == "rejected":
            continue
        sess = c["session"]
        kind = "main" if c["kind"] != "summary" else "summarization"
        row = {"trial": sess, "kind": kind, "prompt_tokens": int(_num(c["prompt"])),
               "cache_read": int(_num(c["cached"])), "cache_write": int(_num(c["cache_write"])),
               "completion_tokens": int(_num(c["output"])), "turn": "", "prompt_delta": "", "compaction": 0}
        if kind == "main":
            n[sess] = n.get(sess, 0) + 1
            row["turn"] = n[sess]
            row["compaction"] = 1 if c["window_start"] else 0
            if sess in prev:
                row["prompt_delta"] = row["prompt_tokens"] - prev[sess]
            prev[sess] = row["prompt_tokens"]
        turns.append(row)
    fields = ["trial", "kind", "turn", "prompt_tokens", "prompt_delta", "cache_read", "cache_write",
              "completion_tokens", "compaction"]
    with (tt / "turns.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(turns)
    with (tt / "summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["trial", "task", "model", "condition", "turns", "compactions"])
        w.writeheader()
        for s in sessions:
            w.writerow({"trial": s["session"], "task": s["root_preview"][:40].replace(",", " ") or s["session"],
                        "model": model, "condition": condition, "turns": s["main_calls"],
                        "compactions": s["compactions"]})
    print(f"token_turns tables -> {tt}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", type=Path)
    ap.add_argument("--prices", help="configs/prices.json row for cost_usd")
    ap.add_argument("--prices-from", type=Path, help="controller spec JSON whose 'prices' row to use")
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--job-dir", type=Path, help="also write <job-dir>/token_turns/{turns,summary}.csv")
    ap.add_argument("--condition", default="", help="condition label for the token_turns summary")
    ap.add_argument("--model", default="", help="model label for the token_turns summary")
    a = ap.parse_args()
    if not a.log.exists():
        print(f"no proxy log: {a.log}", file=sys.stderr)
        return 1
    row = a.prices
    if not row and a.prices_from and a.prices_from.exists():
        row = json.loads(a.prices_from.read_text()).get("prices")
    prices = load_prices(row) if row else None

    calls, sessions = [], {}
    for line in a.log.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("session") is None:
            continue
        d = r.get("decision") or {}
        cost = prices.cost_usd(_num(r.get("cached")), _num(r.get("cache_write")), _num(r.get("output")),
                               _num(r.get("uncached"))) if prices and r.get("prompt") is not None else None
        c = {"session": r["session"], "i": len(calls), "ts": r.get("ts"), "kind": r.get("kind"),
             "action": r.get("action"), "status": r.get("status"), "prompt": r.get("prompt"),
             "cached": r.get("cached"), "cache_write": r.get("cache_write"), "uncached": r.get("uncached"),
             "output": r.get("output"), "reasoning": r.get("reasoning"), "cost_usd": cost,
             "window_idx": d.get("window_idx"), "turn_idx": d.get("turn_idx"), "window_start": d.get("window_start"),
             "compact_now": d.get("compact_now"), "limit": d.get("limit"), "rent": d.get("rent"), "C": d.get("C"),
             "reported_prompt": r.get("reported_prompt"), "reason": d.get("reason")}
        calls.append(c)
        s = sessions.setdefault(r["session"], {"session": r["session"], "main_calls": 0, "summary_calls": 0,
                                               "rejected": 0, "compactions": 0, "compact_signals": 0,
                                               "_prompts": [], "_cached": 0.0, "_prompt_sum": 0.0,
                                               "cost_usd": 0.0, "main_cost_usd": 0.0, "summary_cost_usd": 0.0,
                                               "summary_prompt": 0.0, "summary_output": 0.0,
                                               "root_preview": (r.get("root_preview") or "")[:80]})
        if r.get("action") == "rejected":
            s["rejected"] += 1
            continue
        if c["prompt"] is None:
            continue
        s["_cached"] += _num(c["cached"]); s["_prompt_sum"] += _num(c["prompt"])
        if cost is not None:
            s["cost_usd"] += cost
        if c["kind"] == "summary":
            s["summary_calls"] += 1
            s["summary_prompt"] += _num(c["prompt"]); s["summary_output"] += _num(c["output"])
            if cost is not None:
                s["summary_cost_usd"] += cost
        else:
            s["main_calls"] += 1
            s["_prompts"].append(_num(c["prompt"]))
            if cost is not None:
                s["main_cost_usd"] += cost
            if c["window_start"]:
                s["compactions"] += 1
            if c["compact_now"]:
                s["compact_signals"] += 1

    out_dir = a.out_dir or a.log.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = a.log.name.removesuffix(".jsonl")
    with (out_dir / f"{stem}.calls.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CALL_FIELDS); w.writeheader(); w.writerows(calls)
    rows = []
    for s in sessions.values():
        ps = s.pop("_prompts"); cached = s.pop("_cached"); psum = s.pop("_prompt_sum")
        s["max_prompt"] = max(ps, default=0); s["mean_prompt"] = round(sum(ps) / len(ps)) if ps else 0
        s["cache_hit"] = round(cached / psum, 4) if psum else 0
        for k in ("cost_usd", "main_cost_usd", "summary_cost_usd"):
            s[k] = round(s[k], 4)
        rows.append(s)
    with (out_dir / f"{stem}.sessions.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SESSION_FIELDS); w.writeheader(); w.writerows(rows)
    print(f"{len(calls)} calls, {len(rows)} sessions -> {out_dir}/{stem}.{{calls,sessions}}.csv")
    if a.job_dir:
        write_token_turns(calls, rows, a.job_dir, a.condition, a.model)
    for s in rows:
        print(f"  {s['session'][:20]:20} main={s['main_calls']} summary={s['summary_calls']} "
              f"compactions={s['compactions']} signals={s['compact_signals']} rejected={s['rejected']} "
              f"max_prompt={s['max_prompt']:,.0f} hit={s['cache_hit']:.1%} ${s['cost_usd']} "
              f"(summary ${s['summary_cost_usd']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
