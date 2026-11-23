"""Re-orientation after a compaction: an instruction put behind the compaction summary.

After a compaction the agent reads again what the summary dropped, usually one call per model turn
(optlimit/reread.py measures 3-4 such turns).  `ControllerConfig.reorient` adds an instruction that asks
the agent to work out what it needs and fetch all of it in ONE response (several tool calls, or several
commands in one call): the same lookups in fewer round trips.  Off by default.

  parallel    think what is needed -> decide what the summary lacks -> fetch all of it in one response
  sequential  control: "check the workspace state with the tools", no word about batching
  parallel_v0 the first wording of `parallel` (other mark and phrasing); kept so the runs that used it
              (jobs luna_swem_cx5_*, 2026-11-23) can be reproduced

The note goes where File Carry-over puts its block: the proxy re-inserts it behind the compaction item on
every request of the window (identical text, so the cached prefix stays stable); in-process terminus-2
appends it to the hand-off.

Changes what a compaction hands over (like File Carry-over); when to compact stays with the controller.
Measurement and the cost accounting proposal: docs/parallel_reorientation.md.
"""
from __future__ import annotations

REORIENT_MARK = "[Re-orientation after the context compaction]"

PARALLEL = (REORIENT_MARK + " The summary above may be incomplete or wrong. Before continuing:\n"
            "1. Think about everything you need to know to continue correctly (the task requirements and "
            "constraints, the current contents of the files you are working on, the latest verified test or "
            "build result, the remaining time or budget).\n"
            "2. Decide which of these are missing or uncertain in the summary.\n"
            "3. Fetch ALL of them in ONE response: issue all the needed tool calls together (or chain the "
            "read commands in a single call). Do not look things up one call per turn while re-orienting.\n"
            "Then continue the work.")

SEQUENTIAL = (REORIENT_MARK + " The summary above may be incomplete or wrong. Before continuing, use the "
              "tools to check the current state of the workspace, then continue the work.")

V0_MARK = "[Note added after the context compaction]"

PARALLEL_V0 = (V0_MARK + " Before continuing, re-orient yourself: the summary above may be incomplete or wrong.\n"
               "1. Think about everything you need to know to continue correctly (the task constraints, the current "
               "contents of the files you are working on, the last verified test or build result, what remains to be "
               "done and the remaining budget).\n"
               "2. Decide which of these are missing or uncertain in the summary.\n"
               "3. Fetch ALL of them in ONE step: issue all the needed read commands together in a single response "
               "(several tool calls at once, or one command or script that prints them all). Do not fetch them one "
               "at a time while re-orienting.\n"
               "Then continue the work.")

# every text a block can start with (the proxy's "already inserted" check)
REORIENT_MARKS = (REORIENT_MARK, V0_MARK)

MODES = {"": "", "off": "", "parallel": PARALLEL, "sequential": SEQUENTIAL, "parallel_v0": PARALLEL_V0}


def reorient_block(mode: str | None) -> str:
    """Instruction text for `mode` ("" / "off" -> no block). Unknown modes raise."""
    key = (mode or "").strip().lower()
    if key not in MODES:
        raise ValueError(f"reorient must be one of {sorted(k for k in MODES if k)} or off, got {mode!r}")
    return MODES[key]
