"""Turn the proxy log (and optionally a harbor job directory) into the numbers the
cost model talks about: per session/window L_limit, L_p, g, K, compactions,
read/write/output totals, cost, and — joined with harbor results — accuracy.
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
from collections import defaultdict

from .cost_model import Prices


def load_log(path: str) -> list[dict]:
    recs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    return recs


def _med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def session_stats(recs: list[dict]) -> dict:
    """Per-session reconstruction from forwarded requests (chronological)."""
    main = [r for r in recs if r.get("kind") == "main" and r.get("action") == "forwarded" and r.get("prompt") is not None]
    summ = [r for r in recs if r.get("kind") == "summary" and r.get("action") == "forwarded"]
    rejected = [r for r in recs if r.get("action") == "rejected"]
    P = [r["prompt"] for r in main]
    R = sum((r.get("cached") or 0) for r in main)
    CW = sum((r.get("cache_write") or 0) for r in main)
    U = sum((r.get("uncached") or 0) for r in main)
    O = sum((r.get("output") or 0) for r in main)
    sR = sum((r.get("cached") or 0) for r in summ)
    sW = sum(((r.get("cache_write") or 0) + (r.get("uncached") or 0)) for r in summ)
    sO = sum((r.get("output") or 0) for r in summ)
    # windows from decision.window_start flags
    starts = [i for i, r in enumerate(main) if (r.get("decision") or {}).get("window_start")]
    bounds = [0] + starts + [len(main)]
    windows = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b <= a:
            continue
        seg = P[a:b]
        K = len(seg)
        g = (seg[-1] - seg[0]) / (K - 1) if K > 1 else None
        windows.append({"start": a, "K": K, "P_first": seg[0], "P_last": seg[-1], "g": g,
                        "Lp": seg[0] if a > 0 else None, "L_limit": seg[-1] + (g or 0)})
    limits = [(r.get("decision") or {}).get("limit") for r in main]
    gh = [(r.get("decision") or {}).get("g_hat") for r in main]
    return {
        "session": recs[0].get("session"), "root_preview": recs[0].get("root_preview"), "model": recs[0].get("model"),
        "turns": len(main), "summary_calls": len(summ), "rejections": len(rejected),
        "compactions": len(starts), "max_prompt": max(P) if P else None, "first_prompt": P[0] if P else None,
        "R": R, "cache_write": CW, "uncached": U, "O": O, "sum_R": sR, "sum_W": sW, "sum_O": sO,
        "windows": windows,
        "L_limit_med": _med([w["L_limit"] for w in windows[:-1]]) if len(windows) > 1 else None,
        "Lp_med": _med([w["Lp"] for w in windows if w["Lp"] is not None]),
        "g_realized_med": _med([w["g"] for w in windows]),
        "limit_last": limits[-1] if limits else None, "limit_med": _med(limits), "g_hat_last": gh[-1] if gh else None,
        "t_start": recs[0].get("ts"), "t_end": recs[-1].get("ts"),
    }


def sessions_table(recs: list[dict], min_turns: int = 2) -> list[dict]:
    by = defaultdict(list)
    for r in recs:
        if r.get("session"):
            by[r["session"]].append(r)
    rows = []
    for k, rs in by.items():
        rs.sort(key=lambda r: r.get("ts") or 0)
        s = session_stats(rs)
        if s["turns"] >= min_turns:
            rows.append(s)
    return rows


# ---------------------------------------------------------------- harbor join

def load_harbor_job(job_dir: str) -> list[dict]:
    out = []
    for rf in glob.glob(os.path.join(job_dir, "*", "result.json")):
        try:
            r = json.load(open(rf))
        except Exception:
            continue
        exc = (r.get("exception_info") or {}).get("exception_type")
        if exc == "CancelledError":
            continue
        rw = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        instr = None
        tj = os.path.join(os.path.dirname(rf), "agent", "trajectory.json")
        traj = None
        if os.path.exists(tj):
            try:
                traj = json.load(open(tj))
                for s in traj.get("steps", []):
                    msg = s.get("message")
                    if s.get("source") == "user" and msg and not str(msg).lstrip().startswith("<environment_context>"):
                        instr = msg   # Codex's first user step is the environment context, not the task
                        break
            except Exception:
                traj = None
        if instr is None:
            instr = (r.get("task", {}) or {}).get("instruction") or ""
        out.append({"task": r.get("task_name"), "dir": os.path.dirname(rf), "reward": rw,
                    "outcome": "solved" if rw == 1 else ("timeout" if exc == "AgentTimeoutError" else (exc or "failed")),
                    "instruction": instr or "", "trajectory": traj})
    return out


def _norm(s: str) -> str:
    return " ".join((s or "").split())[:200]


def join_sessions(sessions: list[dict], tasks: list[dict]) -> None:
    """Match a session to a harbor task by the task instruction text appearing in
    the session's first user message (root_preview)."""
    for s in sessions:
        s["task"] = None
        for cand in (s.get("task_preview"), s.get("root_preview")):
            rp = _norm(cand or "")
            if not rp or s["task"]:
                continue
            for t in tasks:
                ins = _norm(t["instruction"])
                if not ins:
                    continue
                probe = ins[:80]
                if probe and probe in rp or rp[:80] in ins:
                    s["task"] = t["task"]
                    s["outcome"] = t["outcome"]
                    s["reward"] = t["reward"]
                    break


def task_previews_from_dump(dump_dir: str) -> dict:
    """session -> last_user_text of its first dumped request (older logs whose root_preview is
    Codex's <environment_context>)."""
    import glob
    out = {}
    for f in sorted(glob.glob(os.path.join(dump_dir, "*.json"))):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        k = d.get("session")
        if k and k not in out and d.get("kind") == "main" and d.get("last_user_text"):
            out[k] = d["last_user_text"][:300]
    return out


def price(s: dict, pr: Prices | None, spr: Prices | None) -> dict:
    if pr is None:
        return {}
    main = pr.cost_usd(read=s["R"], write=s["cache_write"], output=s["O"], uncached=s["uncached"])
    sp = spr or pr
    summ = sp.cost_usd(read=s["sum_R"], write=s["sum_W"], output=s["sum_O"])
    return {"cost_main": main, "cost_summary": summ, "cost_total": main + summ}


def analyze(log_path: str, job_dir: str | None, prices: Prices | None, summary_prices: Prices | None = None) -> dict:
    recs = load_log(log_path)
    sessions = sessions_table(recs)
    tasks = load_harbor_job(job_dir) if job_dir else []
    if tasks:
        prev = task_previews_from_dump(os.path.join(job_dir, "dump"))
        for s in sessions:
            s["task_preview"] = prev.get(s.get("session"))
        join_sessions(sessions, tasks)
    for s in sessions:
        s.update(price(s, prices, summary_prices))
    n = len(sessions)
    tot = {k: sum(s[k] for s in sessions) for k in ("turns", "compactions", "R", "cache_write", "uncached", "O",
                                                      "sum_R", "sum_W", "sum_O", "summary_calls", "rejections")}
    agg = {
        "sessions": n, "tasks_matched": sum(1 for s in sessions if s.get("task")),
        "solved": sum(1 for s in sessions if s.get("reward") == 1),
        "harbor_tasks": len(tasks), "harbor_solved": sum(1 for t in tasks if t["reward"] == 1),
        "totals": tot,
        "L_limit_med": _med([s["L_limit_med"] for s in sessions]),
        "Lp_med": _med([s["Lp_med"] for s in sessions]),
        "g_realized_med": _med([s["g_realized_med"] for s in sessions]),
        "limit_med": _med([s["limit_med"] for s in sessions]),
    }
    if prices:
        agg["cost_total"] = sum(s.get("cost_total", 0) for s in sessions)
        agg["cost_per_turn"] = agg["cost_total"] / tot["turns"] if tot["turns"] else None
    md = [f"| sessions | matched | solved(harbor) | turns | compactions | R(M) | W(M) | O(M) | sumR(M) | sumW(M) | sumO(M) | L_limit med | Lp med | g med | L* med | $ total | $/1000 turns |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
          f"| {n} | {agg['tasks_matched']} | {agg['harbor_solved']}/{len(tasks)} | {tot['turns']} | {tot['compactions']} | "
          f"{tot['R'] / 1e6:.2f} | {(tot['cache_write'] + tot['uncached']) / 1e6:.2f} | {tot['O'] / 1e6:.2f} | "
          f"{tot['sum_R'] / 1e6:.2f} | {tot['sum_W'] / 1e6:.2f} | {tot['sum_O'] / 1e6:.2f} | "
          f"{_fmt(agg['L_limit_med'])} | {_fmt(agg['Lp_med'])} | {_fmt(agg['g_realized_med'])} | {_fmt(agg['limit_med'])} | "
          f"{_fmt(agg.get('cost_total'), '$')} | {_fmt((agg.get('cost_per_turn') or 0) * 1000 if agg.get('cost_per_turn') else None, '$')} |"]
    return {"aggregate": agg, "sessions": sessions, "markdown": "\n".join(md)}


def _fmt(v, pre: str = "") -> str:
    if v is None:
        return "-"
    return f"{pre}{v:,.0f}" if abs(v) >= 100 else f"{pre}{v:.2f}"
