"""Re-orientation after each compaction, from a proxy log (docs/parallel_reorientation.md).

    python analysis/reorient_turns.py jobs/logs/<job>.proxy.jsonl [more logs ...]

Per log: compactions seen, share of windows whose first response issued >= 2 tool calls, mean calls of
that first response, and the re-read calls per window the tracker reported.  `new_tool_calls` of a
request = tool calls of the previous main response, so the width of a window's first response is read
from the second main request of the window.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict


def summarize(path: str) -> dict:
    by = defaultdict(list)
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("kind") == "main" and r.get("session"):
            by[r["session"]].append(r)
    first, reread, modes = [], [], set()
    for recs in by.values():
        for i, r in enumerate(recs):
            w = r.get("reread_window")
            if w and w.get("window", 0) >= 1:
                reread.append(w.get("turns", 0))
            if not w:
                continue                       # not a window start after a compaction
            if r.get("reorient"):
                modes.add(r["reorient"])
            if i + 1 < len(recs) and "new_tool_calls" in recs[i + 1] and not recs[i + 1].get("reread_window"):
                first.append(recs[i + 1]["new_tool_calls"])
    n = len(first)
    return dict(log=path, sessions=len(by), reorient=",".join(sorted(modes)) or "off", windows=n,
                first_calls=round(sum(first) / n, 2) if n else None,
                first_ge2=round(sum(1 for x in first if x >= 2) / n, 2) if n else None,
                reread_turns=round(sum(reread) / len(reread), 2) if reread else None)


if __name__ == "__main__":
    rows = [summarize(p) for p in sys.argv[1:]]
    if not rows:
        sys.exit(__doc__)
    cols = ["log", "sessions", "reorient", "windows", "first_calls", "first_ge2", "reread_turns"]
    print("| " + " | ".join(cols) + " |"); print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")
