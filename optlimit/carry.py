"""File Carry-over (option B): attach the files the agent keeps re-reading behind a compaction summary.

Off by default (`ControllerConfig.carry_files` = token budget, 0 = off).  It only changes what a
compaction hands over; when to compact is the controller's business (see Re-read Charge there).

The re-read tracker (optlimit/reread.py) scores files: +1 for every window that re-read the file
(a read of a file first seen in an earlier window), halved every window.  This module turns those
scores into a text block:

* `snapshot_block` - proxy harnesses (Codex): the latest read output the session already saw for
  each scored file, skipped when the file was edited after that read.
* `disk_block` - terminus-2: the current file contents (the head of each file), read through the task
  environment.
* `whole_block` - "Whole-file Carry-over" (`carry_whole` = token budget, 0 = off): a file re-read in
  `carry_whole_after` consecutive windows (default 2) is handed over complete, read from disk at the
  compaction, and stays so while the agent keeps working on it.  A file is never cut: one that does
  not fit the remaining budget is left to the snapshot / disk block.  Proxy harnesses read the file
  through optlimit/envexec.py (the container the request came from), terminus-2 through its own session.
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


WHOLE_HEADER = ("[Carried over from before the context compaction] Complete current contents of the files you keep "
                "coming back to, read from disk at the moment of the compaction. You already have these files in "
                "full: do not print them again. After you edit one of them, only the part you changed differs "
                "from what is shown here.")


ORDERS = ("score", "reread_edited")


def ranked_files(tracker, order: str = "score") -> list[str]:
    """Scored files, best first.

    order "score" (default): by re-read score.
    order "reread_edited": files the last window both re-read and edited go first (measured 2026-11-18
    on SWE-Marathon, Codex + GPT-6 Luna: 60% of them are re-read in the next window, against 46% of
    the files that were only re-read), then by re-read score.
    """
    if order not in ORDERS:
        raise ValueError(f"carry_order must be one of {ORDERS}, got {order!r}")
    hot = getattr(tracker, "hot", set()) if order == "reread_edited" else set()
    return sorted(tracker.score, key=lambda f: (f not in hot, -tracker.score[f], f))


def snapshot_candidates(tracker, order: str = "score") -> list[tuple[str, object]]:
    """Scored files whose latest read snapshot is still current (no edit after it), best first."""
    out = []
    whole = getattr(tracker, "whole", set())
    for f in ranked_files(tracker, order):
        snap = tracker.snaps.get(f)
        if f in whole or snap is None or tracker.last_edit.get(f, -1) > snap.call_seq:
            continue
        out.append((f, snap))
    return out


def snapshot_block(tracker, budget_tokens: int, per_item_tokens: int = 3000,
                   chars_per_token: float = CHARS_PER_TOKEN, order: str = "score") -> str:
    """Block of earlier read outputs ('' when nothing qualifies); marks the files as carried."""
    if budget_tokens <= 0:
        return ""
    left = int(budget_tokens * chars_per_token)
    cap = int(per_item_tokens * chars_per_token)
    parts, used, files = [], set(), []
    for f, snap in snapshot_candidates(tracker, order):
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
    tracker.carried = set(files) | set(getattr(tracker, "whole", set()))
    if not parts:
        return ""
    return SNAPSHOT_HEADER + "\n\n" + "\n\n".join(parts)


def read_command(path: str, dirs: list[str], nbytes: int) -> str:
    """Shell command printing the first `nbytes` bytes of `path` (relative: looked up in `dirs`, then '.')."""
    if path.startswith("/"):
        return f"[ -f {shlex.quote(path)} ] && head -c {nbytes} -- {shlex.quote(path)}"
    ds = " ".join(shlex.quote(d) for d in (list(dirs) + ["."]))
    return (f"for d in {ds}; do if [ -f \"$d\"/{shlex.quote(path)} ]; then "
            f"head -c {nbytes} -- \"$d\"/{shlex.quote(path)}; exit 0; fi; done; exit 1")


def whole_candidates(tracker, after: int = 2, order: str = "score") -> list[str]:
    """Files re-read in at least `after` consecutive windows (or carried whole and still worked on), best first."""
    return [f for f in ranked_files(tracker, order) if tracker.streak.get(f, 0) >= max(int(after), 1)]


async def whole_block(tracker, run: Callable[[str], Awaitable[tuple[int, str]]], budget_tokens: int, after: int = 2,
                      chars_per_token: float = CHARS_PER_TOKEN, order: str = "score",
                      dirs: list[str] | None = None) -> tuple[str, list[str], list[str]]:
    """Complete current contents of the files in `whole_candidates`, within `budget_tokens`.

    `run(cmd)` executes a shell command in the task environment and returns (return code, stdout).
    Returns (block, files carried whole, candidates left out); marks the files in `tracker.whole` and
    `tracker.carried`.  Left out: missing, empty or binary files and files larger than the budget left."""
    if budget_tokens <= 0:
        return "", [], []
    if dirs is None:
        wd = getattr(tracker, "workdirs", {})
        dirs = sorted(wd, key=lambda d: -wd[d])[:6]
    left = int(budget_tokens * chars_per_token)
    parts, files, skipped = [], [], []
    for path in whole_candidates(tracker, after, order):
        if left < 200:
            skipped.append(path)
            continue
        rc, body = await run(read_command(path, dirs, left + 1))
        body = body or ""
        if rc != 0 or not body.strip() or "\x00" in body or len(body) > left:
            skipped.append(path)
            continue
        parts.append(f"$ cat {path}\n{body}")
        files.append(path)
        left -= len(body) + len(path) + 8
    tracker.whole = set(files)
    tracker.carried = set(getattr(tracker, "carried", set())) | set(files)
    if not parts:
        return "", [], skipped
    return WHOLE_HEADER + "\n\n" + "\n\n".join(parts), files, skipped


async def disk_block(tracker, run: Callable[[str], Awaitable[tuple[int, str]]], dirs: list[str],
                     budget_tokens: int, per_item_tokens: int = 3000,
                     chars_per_token: float = CHARS_PER_TOKEN, order: str = "score") -> tuple[str, list[str]]:
    """Current contents of the scored files.  `run(cmd)` executes a shell command in the task
    environment and returns (return code, stdout); relative paths are looked up in `dirs` then '.'."""
    left = int(budget_tokens * chars_per_token)
    cap = int(per_item_tokens * chars_per_token)
    parts, files = [], []
    whole = set(getattr(tracker, "whole", set()))
    for path in ranked_files(tracker, order):
        if path in whole:                        # already handed over complete (whole_block)
            continue
        room = min(cap, left)
        if room < 400:
            break
        rc, body = await run(read_command(path, dirs, room + 1))
        body = body or ""
        if rc != 0 or not body.strip():
            continue
        if len(body) > room:
            body = body[:room] + "\n[... truncated ...]"
        parts.append(f"===== {path} =====\n{body}")
        files.append(path)
        left -= len(body) + len(path) + 14
    tracker.carried = set(files) | whole
    if not parts:
        return "", []
    return DISK_HEADER + "\n\n" + "\n\n".join(parts), files
