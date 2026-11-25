"""Calibration: initial values of the cost model from a short pilot run of the same task.

A formula / balance run starts from a prior row of configs/priors.json and corrects it online.  A prior
measured on another model or harness is wrong until the first compactions of every trial.  Calibration
replaces it: run the task briefly once (the pilot, with a low fixed limit so that it compacts a few
times), read the pilot's proxy log, and write a prior row with what was measured:

    g             prompt growth per main call inside a window (total growth / number of steps)
    L0            cached part of the first prompt after a compaction (fixed prefix)
    Lp            uncached part of that prompt (hand-off written anew)
    summary_out   output tokens of a summary call
    gamma         1 + (alpha - 1) x uncached share of the summary call's input
    m             summary input / prompt of the call before it
    reread_turns  re-read model calls per window (optlimit/reread.py), the prior of the Re-read Charge

Medians over compactions (g: ratio of sums).  A value the pilot could not measure (no compaction, no
finished window) is taken from --base and listed under "_from_base".

    python -m optlimit.calibrate jobs/logs/<pilot>.proxy.jsonl --name cal-<task> --prices <row> \
        --base codex-luna-medium --write

The row is then used like any prior: make_config.py --formula cal-<task> --learn.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"
FIELDS = ("g", "L0", "Lp", "summary_out", "gamma", "m", "reread_turns")


def read_log(path) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("status") == 200 and r.get("prompt") and r.get("kind") in ("main", "summary"):
                rows.append(r)
    return rows


def measure(rows: list[dict], alpha: float) -> dict:
    """Measured values from proxy log rows (one or more sessions); keys of FIELDS that could be measured,
    plus the counts they rest on."""
    by_session: dict[str, list[dict]] = {}
    for r in rows:
        by_session.setdefault(r.get("session") or "", []).append(r)
    growth = steps = 0.0
    l0, lp, so, unc, m, rr = [], [], [], [], [], []
    n_main = 0
    for sess in by_session.values():
        prev_main = None
        after_summary = False
        for r in sess:
            if r["kind"] == "summary":
                so.append(float(r.get("output") or 0))
                unc.append(max(r["prompt"] - (r.get("cached") or 0), 0) / r["prompt"])
                if prev_main is not None:
                    m.append(r["prompt"] / prev_main["prompt"])
                after_summary, prev_main = True, None
                continue
            n_main += 1
            if after_summary:
                l0.append(float(r.get("cached") or 0))
                lp.append(float(max(r["prompt"] - (r.get("cached") or 0), 0)))
                after_summary = False
            elif prev_main is not None:
                growth += r["prompt"] - prev_main["prompt"]
                steps += 1
            w = r.get("reread_window")
            if isinstance(w, dict) and w.get("turns") is not None:
                rr.append(float(w["turns"]))
            prev_main = r
    out: dict = {"_n": {"sessions": len(by_session), "main_calls": n_main, "compactions": len(so),
                        "windows_with_reread_count": len(rr)}}
    if steps >= 5 and growth > 0:
        out["g"] = round(growth / steps)
    if l0:
        out["L0"] = round(statistics.median(l0))
        out["Lp"] = round(statistics.median(lp))
    if so:
        out["summary_out"] = round(statistics.median(so))
        out["gamma"] = round(1 + (alpha - 1) * statistics.median(unc), 2)
        out["_summary_uncached_share"] = round(statistics.median(unc), 3)
    if m:
        out["m"] = round(statistics.median(m), 2)
    if rr:
        out["reread_turns"] = round(statistics.mean(rr), 2)
    return out


def prior_row(measured: dict, base: dict, source: str) -> dict:
    row = {"source": source}
    missing = []
    for k in FIELDS:
        if k in measured:
            row[k] = measured[k]
        elif base.get(k) is not None:
            row[k] = base[k]
            missing.append(k)
        elif k != "reread_turns":
            missing.append(k)
    row["_n"] = measured["_n"]
    if "_summary_uncached_share" in measured:
        row["_summary_uncached_share"] = measured["_summary_uncached_share"]
    if missing:
        row["_from_base"] = missing
    return row


def write_row(name: str, row: dict, config_dir: Path = CONFIG_DIR) -> None:
    path = Path(config_dir) / "priors.json"
    data = json.loads(path.read_text())
    data["harness"][name] = row
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")   # the file's own layout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prior row from the proxy log(s) of a pilot run")
    ap.add_argument("logs", nargs="+", help="jobs/logs/<job>.proxy.jsonl of the pilot (several are pooled)")
    ap.add_argument("--name", required=True, help="name of the priors.json row to write")
    ap.add_argument("--prices", required=True, help="prices.json row (alpha turns the uncached share into gamma)")
    ap.add_argument("--base", required=True, help="priors.json row for what the pilot could not measure")
    ap.add_argument("--min-compactions", type=int, default=1,
                    help="fail when the pilot has fewer compactions than this (default 1)")
    ap.add_argument("--write", action="store_true", help="store the row in configs/priors.json")
    ap.add_argument("--config-dir", default=str(CONFIG_DIR))
    a = ap.parse_args(argv)

    from .controller import load_prices
    alpha = load_prices(a.prices, Path(a.config_dir)).alpha
    base = json.loads((Path(a.config_dir) / "priors.json").read_text())["harness"][a.base]
    rows = [r for p in a.logs for r in read_log(p)]
    measured = measure(rows, alpha)
    row = prior_row(measured, base, f"calibration pilot: {', '.join(Path(p).name for p in a.logs)}; "
                                    f"prices {a.prices} (alpha {alpha:g}); base {a.base}")
    print(json.dumps({a.name: row}, indent=2, ensure_ascii=False))
    if measured["_n"]["compactions"] < a.min_compactions:
        print(f"pilot has {measured['_n']['compactions']} compaction(s), fewer than --min-compactions "
              f"{a.min_compactions}: row not written", flush=True)
        return 2
    if a.write:
        write_row(a.name, row, Path(a.config_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
