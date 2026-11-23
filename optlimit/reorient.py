"""Parallel re-orientation: a note behind the compaction summary that asks the agent to fetch what
it needs to continue in ONE step instead of one read per model call (docs/parallel_reorientation.md).

Only the wording changes; the harness is not forced to do anything.  The note goes where File
Carry-over puts its block: the Codex proxy re-inserts it behind the compaction item on every request
of the window (identical text, so the prompt-cache prefix stays stable); in-process terminus-2
appends it to the hand-off.  `ControllerConfig.reorient` selects the wording:

* "parallel": think about what is needed, decide what the summary lacks, fetch all of it in one
  response (several tool calls, or one command / script that prints everything).
* "plain": the control wording - re-orient, with no instruction about how to fetch.
* "off" (default): no note.

The task-specific list of the measured prompt (kernel file, cycle count, ...) is replaced by a
general one, as the document asks for other tasks.
"""
from __future__ import annotations

MARK = "[Note added after the context compaction]"

PARALLEL = (
    MARK + " Before continuing, re-orient yourself: the summary above may be incomplete or wrong.\n"
    "1. Think about everything you need to know to continue correctly (the task constraints, the current "
    "contents of the files you are working on, the last verified test or build result, what remains to be "
    "done and the remaining budget).\n"
    "2. Decide which of these are missing or uncertain in the summary.\n"
    "3. Fetch ALL of them in ONE step: issue all the needed read commands together in a single response "
    "(several tool calls at once, or one command or script that prints them all). Do not fetch them one "
    "at a time while re-orienting.\n"
    "Then continue the work."
)

PLAIN = (
    MARK + " Before continuing, re-orient yourself: the summary above may be incomplete or wrong. Use the "
    "tools to check the current state of the workspace, then continue."
)

MODES = ("off", "parallel", "plain")


def reorient_text(mode: str | None) -> str:
    """The note for `mode` ("" for off / None)."""
    m = (mode or "off").lower()
    if m in ("off", "none", "false", ""):
        return ""
    if m == "parallel":
        return PARALLEL
    if m == "plain":
        return PLAIN
    raise ValueError(f"optlimit: unknown reorient mode {mode!r}; known: {MODES}")
