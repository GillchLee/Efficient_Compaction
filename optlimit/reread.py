"""Re-reads after a compaction: detecting them.  Shared by Re-read Charge (controller, C += R),
Net rent (controller, balance mode) and File Carry-over (optlimit/carry.py).

A compaction drops tool output from the context, so the agent reads again files it had already
seen.  Measured on Codex + GPT-6 Luna (2026-11): 3-4 such model calls per compaction, half of all
calls at a 35-40K window, concentrated on the few files the agent is working on.

`RereadTracker` follows one session.  Feed it every tool call the harness shows (`observe`), tell
it when a window starts (`start_window`); it reports

* how many model calls of the finished window re-read a file last seen in an earlier window
  (the controller charges them to the compaction: `ControllerConfig.reread_charge`), and
* per-file re-read scores, which File Carry-over (optlimit/carry.py) turns into a block for the
  next window, and `streak`: for how many consecutive windows a file has been re-read (File Carry-over
  hands a file over whole from `carry_whole_after` such windows on).

A call counts as a re-read when it is a read-type call (cat/sed -n/head/rg/grep/ls/git diff/...,
or a Read/Grep/Glob tool) that names a file path which already appeared in a call of an earlier
window and has not appeared yet in the current one.  It is a heuristic over command text: reading
back a file after editing it is included.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

READ_CMD = re.compile(r"^\s*(?:cd\s+\S+\s*&&\s*)?(cat|sed\s+-n|head|tail|rg|grep|ls|find|wc|nl|tree|stat|file|less|more|bat|"
                      r"git\s+(?:diff|status|log|show|grep))\b")
EDIT_TXT = re.compile(r"apply_patch|\*\*\* (?:Update|Add|Delete) File|sed\s+-i|\btee\s|cat\s*>|>>?\s*[\w./-]+\.\w+")
PATCH_FILE = re.compile(r"\*\*\* (?:Update|Add|Delete) File:\s*([\w./@+-]+)")
PATH = re.compile(r"[\w./@+-]*[\w@+-]\.(?:rs|py|pyi|ts|tsx|js|jsx|mjs|cjs|rb|toml|json|md|go|c|h|cc|cpp|hpp|java|kt|scala|"
                  r"yaml|yml|txt|sh|css|scss|html|csv|tsv|cfg|ini|sql|xml|tex|lean|v|ml|mli|R|ipynb|log|conf|lock|proto|"
                  r"swift|php|lua|pl|ex|exs|hs|zig|vue|svelte|gradle|cmake|mk)\b|[\w./-]*(?:Makefile|Dockerfile)\b")
READ_TOOLS = {"read", "grep", "glob", "view", "ls", "read_file", "list_dir", "search"}
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "str_replace_editor", "str_replace_based_edit_tool",
              "write_file", "create_file"}
WORKDIR = re.compile(r'\\?["\']?workdir\\?["\']?\s*:\s*\\?["\'](/[^"\'\\]{1,200})')   # exec_command({cmd:..., workdir:"/app"})
CMD_IN_JS = re.compile(r'cmd:\s*("(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`|\'(?:[^\'\\]|\\.)*\')', re.S)


def command_text(name: str, text: str) -> str:
    """The shell command inside a tool call's raw arguments, when there is one."""
    m = CMD_IN_JS.search(text)                      # Codex exec: tools.exec_command({cmd:"..."})
    if m:
        return m.group(1)[1:-1].replace('\\"', '"').replace("\\n", "\n")
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(obj, dict):
        for k in ("cmd", "command", "keystrokes"):
            v = obj.get(k)
            if isinstance(v, list):
                v = " ".join(str(x) for x in v)
            if isinstance(v, str):
                return v
    return text


def classify_call(name: str, text: str) -> tuple[bool, bool, list[str]]:
    """(is_read, is_edit, paths) of one tool call from its tool name and raw arguments."""
    lname = (name or "").lower()
    cmd = command_text(name, text or "")
    is_edit = lname in EDIT_TOOLS or bool(EDIT_TXT.search(text or ""))
    is_read = not is_edit and (lname in READ_TOOLS or bool(READ_CMD.match(cmd)))
    paths = PATCH_FILE.findall(text or "") if "*** " in (text or "") else []
    paths += PATH.findall(text or "")
    out, seen = [], set()
    for p in paths:
        p = p.strip("\"'`,;:()[]{}")
        while p.startswith("./"):
            p = p[2:]
        if p and p not in seen and len(p) < 300:
            seen.add(p)
            out.append(p)
    return is_read, is_edit, out


@dataclass
class _Snap:
    call_seq: int
    command: str
    output: str
    paths: tuple[str, ...]


@dataclass
class RereadTracker:
    decay: float = 0.5            # score of a file is halved every window it is not re-read / touched
    drop_below: float = 0.25
    window: int = 0
    seq: int = 0                  # tool calls seen
    reread_turns: int = 0         # model calls of the current window that re-read an earlier-window file
    reread_cost: float = 0.0      # their cost (whatever unit the caller passes to note_turn_cost)
    last_seen: dict = field(default_factory=dict)      # canonical path -> window of the last call naming it
    last_edit: dict = field(default_factory=dict)      # canonical path -> seq of the last edit
    score: dict = field(default_factory=dict)          # canonical path -> re-read score
    snaps: dict = field(default_factory=dict)          # canonical path -> latest read snapshot
    carried: set = field(default_factory=set)          # paths in the carry block of the current window
    streak: dict = field(default_factory=dict)         # canonical path -> consecutive finished windows that re-read it
    whole: set = field(default_factory=set)            # paths carried as whole files in the current window
    workdirs: dict = field(default_factory=dict)       # working directory -> calls that ran there (relative paths)
    hot: set = field(default_factory=set)              # files the last finished window re-read AND edited
    _canon: dict = field(default_factory=dict)
    _reread_now: set = field(default_factory=set)      # files re-read in the current window
    _touched_now: set = field(default_factory=set)
    _edited_now: set = field(default_factory=set)      # files edited in the current window
    _seen_ids: set = field(default_factory=set)
    _pending_turn: bool = False                        # a re-read was seen among the calls of the last response
    history: list = field(default_factory=list)        # (window, reread_turns, reread_cost) of finished windows

    # ------------------------------------------------------------ paths
    def canon(self, p: str) -> str:
        c = self._canon.get(p)
        if c is not None:
            return c
        c = p
        for k in self.last_seen:                        # /app/src/x.rs and src/x.rs are the same file
            if k == p or k.endswith("/" + p) or p.endswith("/" + k):
                c = k
                break
        self._canon[p] = c
        return c

    # ------------------------------------------------------------ feeding
    def observe(self, call_id: str | None, name: str, text: str, output: str | None = None) -> bool:
        """One tool call (and its output when known). Returns True when it is a re-read."""
        if call_id is not None:
            if call_id in self._seen_ids:
                return False
            self._seen_ids.add(call_id)
        self.seq += 1
        for d in WORKDIR.findall(text or ""):
            self.workdirs[d] = self.workdirs.get(d, 0) + 1
        is_read, is_edit, paths = classify_call(name, text)
        files = [self.canon(p) for p in paths]
        reread = False
        if is_read:
            for f in files:
                prev = self.last_seen.get(f)
                if prev is not None and prev < self.window:
                    reread = True
                    self._reread_now.add(f)
            if output and files:
                snap = _Snap(self.seq, command_text(name, text)[:2000], output, tuple(files))
                for f in files:
                    self.snaps[f] = snap
        if is_edit:
            for f in files:
                self.last_edit[f] = self.seq
                self._edited_now.add(f)
        for f in files:
            self.last_seen[f] = self.window
            self._touched_now.add(f)
        if reread:
            self._pending_turn = True
        return reread

    def close_turn(self, cost: float = 0.0) -> bool:
        """Call once per model call after feeding the tool calls it issued; `cost` is that call's cost."""
        hit, self._pending_turn = self._pending_turn, False
        if hit:
            self.reread_turns += 1
            self.reread_cost += float(cost)
        return hit

    def start_window(self) -> tuple[int, int, float]:
        """A compaction happened. Returns (finished window index, its re-read turns, their cost)."""
        done = (self.window, self.reread_turns, self.reread_cost)
        self.history.append(done)
        for f in set(self.score) | self._reread_now:
            s = self.score.get(f, 0.0) * self.decay
            if f in self._reread_now:
                s += 1.0
            elif f in self.carried and f in self._touched_now:
                s += 0.5                                  # carried and still in use: keep carrying
            if s < self.drop_below:
                self.score.pop(f, None)
            else:
                self.score[f] = s
        for f in set(self.streak) | self._reread_now:
            if f in self._reread_now:
                self.streak[f] = self.streak.get(f, 0) + 1
            elif f in self.whole and f in self._touched_now:
                pass                                      # carried whole and still worked on: stays whole
            else:
                self.streak.pop(f, None)
        if self.window == 0:
            # nothing can be a re-read before the first compaction: seed with the files edited so far
            for f in self.last_edit:
                self.score.setdefault(f, 0.5)
        self.hot = self._reread_now & self._edited_now
        self.window += 1
        self.reread_turns, self.reread_cost = 0, 0.0
        self._reread_now, self._touched_now, self._edited_now, self.carried = set(), set(), set(), set()
        self.whole = set()
        self._pending_turn = False
        return done

    # ------------------------------------------------------------ File Carry-over (optlimit/carry.py)
    def carry_candidates(self, order: str = "score") -> list:
        from .carry import snapshot_candidates
        return snapshot_candidates(self, order)

    def carry_block(self, budget_tokens: int, per_item_tokens: int = 3000, chars_per_token: float = 3.5,
                    order: str = "score") -> str:
        from .carry import snapshot_block
        return snapshot_block(self, budget_tokens, per_item_tokens, chars_per_token, order)
