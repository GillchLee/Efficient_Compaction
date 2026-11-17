"""File Carry-over (option B): attach the files the agent keeps re-reading behind a compaction summary.

Off by default (`ControllerConfig.carry_files` = token budget, 0 = off).  It only changes what a
compaction hands over; when to compact is the controller's business (see Re-read Charge there).

The re-read tracker (optlimit/reread.py) scores files: +1 for every window that re-read the file
(a read of a file first seen in an earlier window), halved every window.  This module turns those
scores into a text block:

* `snapshot_block` - proxy harnesses (Codex): the latest read output the session already saw for
  each scored file, skipped when the file was edited after that read.
* `disk_block` - terminus-2: the current file contents, read through the task environment.
"""
from __future__ import annotations

import shlex
from typing import Awaitable, Callable

CHARS_PER_TOKEN = 3.5
SNAPSHOT_HEADER = ("[Carried over from before the context compaction] Output of earlier read commands for the files "
                   "you were working with. These files have not been modified since the output was captured. "
                   "Use it instead of reading the same content again; read a file only for parts not shown here.")
DISK_HEADER = ("[Carried over from before the context compaction] Current contents of the files you were working "
               "with, read from disk just now. Use them instead of printing the same files again; read a file only "
               "for parts not shown here.")


def ranked_files(tracker) -> list[str]:
    """Scored files, best first."""
    return sorted(tracker.score, key=lambda f: (-tracker.score[f], f))


def snapshot_candidates(tracker) -> list[tuple[str, object]]:
    """Scored files whose latest read snapshot is still current (no edit after it), best first."""
    out = []
    for f in ranked_files(tracker):
        snap = tracker.snaps.get(f)
        if snap is None or tracker.last_edit.get(f, -1) > snap.call_seq:
            continue
        out.append((f, snap))
    return out


def snapshot_block(tracker, budget_tokens: int, per_item_tokens: int = 3000,
                   chars_per_token: float = CHARS_PER_TOKEN) -> str:
    """Block of earlier read outputs ('' when nothing qualifies); marks the files as carried."""
    if budget_tokens <= 0:
        return ""
    left = int(budget_tokens * chars_per_token)
    cap = int(per_item_tokens * chars_per_token)
    parts, used, files = [], set(), []
    for f, snap in snapshot_candidates(tracker):
        if snap.call_seq in used:
            files.append(f)
            continue
        body = snap.output
        room = min(cap, left - len(snap.command) - 40)
        if room < 400:
            break
        if len(body) > room:
            body = body[:room] + "\n[... truncated ...]"
        parts.append(f"$ {snap.command}\n{body}")
        left -= len(body) + len(snap.command) + 40
        used.add(snap.call_seq)
        files.append(f)
    if not parts:
        return ""
    tracker.carried = set(files)
    return SNAPSHOT_HEADER + "\n\n" + "\n\n".join(parts)


async def disk_block(tracker, run: Callable[[str], Awaitable[tuple[int, str]]], dirs: list[str],
                     budget_tokens: int, per_item_tokens: int = 3000,
                     chars_per_token: float = CHARS_PER_TOKEN) -> tuple[str, list[str]]:
    """Current contents of the scored files.  `run(cmd)` executes a shell command in the task
    environment and returns (return code, stdout); relative paths are looked up in `dirs` then '.'."""
    left = int(budget_tokens * chars_per_token)
    cap = int(per_item_tokens * chars_per_token)
    parts, files = [], []
    for path in ranked_files(tracker):
        room = min(cap, left)
        if room < 400:
            break
        if path.startswith("/"):
            cmd = f"[ -f {shlex.quote(path)} ] && head -c {room + 1} -- {shlex.quote(path)}"
        else:
            ds = " ".join(shlex.quote(d) for d in (list(dirs) + ["."]))
            cmd = (f"for d in {ds}; do if [ -f \"$d\"/{shlex.quote(path)} ]; then "
                   f"head -c {room + 1} -- \"$d\"/{shlex.quote(path)}; exit 0; fi; done; exit 1")
        rc, body = await run(cmd)
        body = body or ""
        if rc != 0 or not body.strip():
            continue
        if len(body) > room:
            body = body[:room] + "\n[... truncated ...]"
        parts.append(f"===== {path} =====\n{body}")
        files.append(path)
        left -= len(body) + len(path) + 14
    if not parts:
        return "", []
    tracker.carried = set(files)
    return DISK_HEADER + "\n\n" + "\n\n".join(parts), files
