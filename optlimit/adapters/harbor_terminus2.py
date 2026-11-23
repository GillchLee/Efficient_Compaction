"""terminus-2 whose compaction *timing* is decided by optlimit, in-process (no proxy).

Harbor adapter. The harness-independent part is optlimit.controller (see docs/adapter_guide.md);
this file only (1) reads the API usage of the previous call, (2) asks the controller, (3) calls
terminus-2's own summarization, (4) logs the decision. Hand-off prompt variants live in
terminus2_handoff.py. Importable as optlimit.harbor_agent:OptlimitTerminus2 (shim) too.

Use it in a Harbor job config in place of `name: terminus-2`:

    agents:
      - import_path: optlimit.harbor_agent:OptlimitTerminus2
        model_name: anthropic/claude-sonnet-5-5
        kwargs:
          parser_name: json
          enable_summarize: true
          proactive_summarization_threshold: 8000   # native check stays as a safety net
          optlimit:
            mode: fixed          # fixed | formula | off
            fixed_limit: 80000   # compact once the API-reported prompt reaches 80k
            lookahead: 0         # 0: P_t >= L ; 1: P_t + g_hat >= L (predict the next prompt)
            # formula mode instead:  mode: formula, prior: terminus2-sonnet, prices: claude-sonnet-5-5
            # (any ControllerConfig field can be given here and overrides the prior)

How it works. terminus-2 calls `_check_proactive_summarization()` at the start of every
episode, before the next LLM call. The stock check compares litellm's local token count of
the chat with `proactive_summarization_threshold`. This subclass first asks the optlimit
controller, feeding it the prompt_tokens / cache_read the API reported for the *previous*
call (`chat.last_usage`, added by patches/lhtb-7ca0cac/01-usage-tracking.patch). When the
controller says "compact now" it runs terminus-2's own `_summarize()` - the same three-call
summarization the stock agent uses - so only the moment of compaction changes. Otherwise the
stock threshold check still runs (safety net). Every decision is appended to
`<agent logs>/optlimit_decisions.jsonl`; analysis/token_turns.py joins it to turns.csv.

`mode: off` never compacts on its own (stock behaviour) but still logs the prompts, so the
same agent class can run the native baseline.

Harbor versions. On the patched LHTB fork the usage comes from `chat.last_usage`. Stock Harbor
(0.25 profile: SWE-Marathon, SWE-bench, ...) has no such field, so the adapter keeps the usage of
the response `_query_llm` returned; nothing else differs. The `handoff:` length targets need the
prompt hooks of patches/lhtb-7ca0cac/02 and are refused on stock Harbor (`mode: single` works).

Re-reads and carry-over. The adapter feeds the commands of every reply to the re-read tracker
(optlimit/reread.py): the controller charges the re-read calls to the compaction (Re-read Charge).
`carry_files: N` appends the head of the files the agent keeps re-reading to the hand-off (File
Carry-over, read from disk through the session); `carry_whole: N` hands a file over complete once
it was re-read in `carry_whole_after` consecutive windows (Whole-file Carry-over). The size of the
whole files goes to the controller (`note_carry`), so the limit follows from the first turn.

Adaptive terms. `learn_lp / learn_summary_out / learn_gamma: true` in the optlimit block make
the controller replace the prior Lp, summary output and cache multiplier by what this run's
compactions show (the agent feeds every summarization call's usage to the controller through
`_update_subagent_metrics`). `prices_reload: true` makes the agent re-read its `prices` row
from configs/prices.json whenever the file changes and hand the new alpha/beta to the
controller, so a price change (or a cache-TTL change) moves L* mid-run; each decision record
carries the alpha/beta/gamma/Lp/summary_out it used.

Hand-off size (Lp). The post-compaction prompt is system prompt + task + summary + terminal
screen + questions + answers. An optional `handoff:` block sets length targets for the three
summarization calls (stock prompts when absent):

          handoff:
            summary_words: 2600   # "approximately N words" in the summary prompt
            min_questions: 12     # "at least N questions" (stock: five)
            answer_words: 2000    # "approximately N words in total" in the answer prompt

`handoff.mode: single` replaces terminus-2's three calls (summary, questions, answers: m ~ 2.2
full-context reads and ~4K output tokens per compaction) by ONE structured summary call
(m = 1) whose output is the whole hand-off: the next window starts from
[system, "task + structured summary + terminal screen + continue"].  `summary_words`
sets its length.  Everything else (trajectory files, metrics, decision log) is unchanged.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from harbor.agents.terminus_2.terminus_2 import Terminus2

from ..controller import LimitController, config_from_spec, load_prices
from ..dialects import _keystrokes
from ..carry import CHARS_PER_TOKEN, disk_block, whole_block
from ..reorient import reorient_text
from ..reread import RereadTracker

import re

_CD = re.compile(r"(?:^|[;&|\n]\s*)cd\s+(\S+)")
from .terminus2_handoff import HandoffPromptsMixin

class OptlimitTerminus2(HandoffPromptsMixin, Terminus2):

    # Harbor >= 0.20 validates agent kwargs against a pydantic options_model (extra = forbid), also in a
    # preflight before the constructor runs: declare the two kwargs of this adapter there. The LHTB fork
    # has no options_model and takes any kwarg.
    if getattr(Terminus2, "options_model", None) is not None:
        options_model = type("OptlimitTerminus2Options", (Terminus2.options_model,),
                             {"__annotations__": {"optlimit": dict | None, "handoff": dict | None},
                              "optlimit": None, "handoff": None, "__module__": __name__})

    def __init__(self, logs_dir: Path, model_name: str | None = None, optlimit: dict | None = None,
                 handoff: dict | None = None, **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        optlimit = dict(optlimit or {})
        self._ol_prices_row = optlimit.get("prices") if optlimit.get("prices_reload") else None
        self._ol_prices_mtime = self._prices_mtime() if self._ol_prices_row else None
        self._ol_cfg = config_from_spec(optlimit)
        self._handoff = self.validate_handoff(handoff)
        if any(k != "mode" for k in self._handoff) and self._handoff.get("mode", "stock") != "single" \
                and not hasattr(Terminus2, "_summary_prompt"):
            raise ValueError("handoff length targets need the patched LHTB Harbor fork (patches/lhtb-7ca0cac/02); "
                             "this Harbor has no prompt hooks. Use handoff.mode: single or drop the block.")
        self._ctl = LimitController(self._ol_cfg)
        self._ol_key = "main"
        self._ol_seen = None          # last UsageInfo object the controller has consumed
        self._ol_usage = None         # usage of the last main response (stock Harbor: Chat has no last_usage)
        self._ol_boundary = False     # a summarization happened: the next prompt starts a window
        self._ol_rr = RereadTracker()  # re-reads after a compaction (cost term; carry-over when carry_files > 0)
        self._ol_dirs: list[str] = []  # directories the agent cd'ed to, newest first
        self._ol_path = Path(logs_dir) / "optlimit_decisions.jsonl"
        self.logger.info(f"optlimit controller: {self._ol_cfg}; handoff: {self._handoff or 'stock'}")

    # ------------------------------------------------------------------ logging
    def _ol_log(self, rec: dict) -> None:
        try:
            self._ol_path.parent.mkdir(parents=True, exist_ok=True)
            with self._ol_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:  # never break the run because of the log
            self.logger.warning(f"optlimit: cannot write {self._ol_path}: {e}")

    # ------------------------------------------------------------------ adaptivity
    @staticmethod
    def _prices_path() -> Path:
        return Path(__file__).resolve().parents[2] / "configs" / "prices.json"

    def _prices_mtime(self) -> float | None:
        try:
            return self._prices_path().stat().st_mtime
        except OSError:
            return None

    def _maybe_reload_prices(self) -> dict | None:
        """Re-read the prices row when configs/prices.json changed; returns the update record."""
        if not self._ol_prices_row:
            return None
        mtime = self._prices_mtime()
        if mtime is None or mtime == self._ol_prices_mtime:
            return None
        self._ol_prices_mtime = mtime
        try:
            pr = load_prices(self._ol_prices_row, config_dir=self._prices_path().parent)
        except (OSError, KeyError, ValueError) as e:
            self.logger.warning(f"optlimit: prices reload failed: {e}")
            return None
        if pr.alpha == self._ol_cfg.alpha and pr.beta == self._ol_cfg.beta:
            return None
        self._ctl.update_prices(pr.alpha, pr.beta, note=f"reload {self._ol_prices_row}")
        self.logger.info(f"optlimit: prices reloaded alpha={pr.alpha} beta={pr.beta}")
        return {"alpha": pr.alpha, "beta": pr.beta}

    def _update_subagent_metrics(self, usage_info) -> None:
        super()._update_subagent_metrics(usage_info)
        if usage_info:
            self._ctl.note_summary_usage(self._ol_key, float(usage_info.prompt_tokens or 0),
                                         cached=float(usage_info.cache_tokens or 0),
                                         completion=float(usage_info.completion_tokens or 0))

    # ------------------------------------------------------------------ the hook
    async def _query_llm(self, chat, prompt, *args, **kwargs):
        resp = await super()._query_llm(chat, prompt, *args, **kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._ol_usage = usage
        return resp

    async def _check_proactive_summarization(self, chat, original_instruction, session):
        usage = getattr(chat, "last_usage", None) or self._ol_usage
        if usage is None or usage is self._ol_seen:
            # first episode of a chat, or the previous episode made no new main call
            return await super()._check_proactive_summarization(chat, original_instruction, session)
        self._ol_seen = usage
        reloaded = self._maybe_reload_prices()

        d = self._ctl.on_prompt(self._ol_key, "main", float(usage.prompt_tokens),
                                cached=float(usage.cache_tokens or 0), window_start_hint=self._ol_boundary)
        if d.window_start and not self._ol_boundary:
            self._ctl.note_carry(self._ol_key, 0.0)   # a compaction the adapter did not run: nothing was carried
        self._ol_boundary = False
        rec = {"ts": time.time(), "prompt": usage.prompt_tokens, "cached": usage.cache_tokens,
               **d.as_dict(), "action": None}
        try:
            if self._ol_note_commands(chat, usage):
                rec["reread_turn"] = True
                # its output is in the next prompt: Net rent leaves that growth out of the rent
                self._ctl.note_reread_growth(self._ol_key)
        except Exception as e:  # tracking must never break the run
            self.logger.warning(f"optlimit: re-read tracking failed: {e}")
        if reloaded:
            rec["prices_reloaded"] = reloaded

        result = None
        if d.compact_now:
            try:
                result = await self._summarize(chat, original_instruction, session)
                rec["action"] = "summarize"
            except Exception as e:  # same handling as the stock check
                self.logger.error(f"optlimit: error in summarizing: {e}")
                rec["action"] = f"summarize_failed: {e}"
                self._ctl.session(self._ol_key).pending_compact = False
        else:
            result = await super()._check_proactive_summarization(chat, original_instruction, session)
            if result:
                rec["action"] = "native_threshold"
        if result:
            self._ctl.note_kind(self._ol_key, "summary")
            self._ol_boundary = True
            w, turns, cost = self._ol_rr.start_window()
            if w >= 1:
                self._ctl.note_reread(self._ol_key, turns, cost)
            rec["reread_window"] = {"window": w, "turns": turns, "cost": round(cost)}
            if w >= 1:
                rec["phase_window"] = dict(self._ol_rr.last_phase)
            c = self._ol_cfg
            whole = block = ""
            if c.carry_whole > 0 and w >= 1:
                # files re-read window after window: complete, from the task environment's disk
                try:
                    whole, wfiles, wskip = await whole_block(self._ol_rr, self._ol_env_run(session), c.carry_whole,
                                                             c.carry_whole_after, order=c.carry_order,
                                                             dirs=self._ol_dirs)
                    rec["carry_whole"] = {"chars": len(whole), "files": wfiles, "left_out": wskip[:40]}
                except Exception as e:
                    self.logger.warning(f"optlimit: whole-file carry failed: {e}")
                    whole = ""
            self._ctl.note_carry(self._ol_key, len(whole) / CHARS_PER_TOKEN)
            if c.carry_files > 0:
                try:
                    block, files = await self._ol_carry_block(session)
                    if block:
                        rec["carry"] = {"chars": len(block), "files": files}
                except Exception as e:
                    self.logger.warning(f"optlimit: carry-over failed: {e}")
                    block = ""
            note = reorient_text(c.reorient)      # parallel re-orientation: last, it refers to what is above
            if note:
                rec["reorient"] = c.reorient
            extra = "\n\n".join(x for x in (whole, block, note) if x)
            if extra:
                result = (result[0] + "\n\n" + extra, result[1])
        self._ol_log(rec)
        return result

    # ------------------------------------------------------------------ re-reads after a compaction
    def _ol_note_commands(self, chat, usage) -> bool:
        """Feed the commands of the newest assistant reply to the re-read tracker."""
        last = next((m for m in reversed(chat.messages) if isinstance(m, dict) and m.get("role") == "assistant"), None)
        if last is None:
            return False
        content = last.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        for ks in _keystrokes(text):
            self._ol_rr.observe(None, "keystrokes", ks)
            for m in _CD.finditer(ks):          # remember where the agent works: relative paths resolve there
                d = m.group(1).strip("\"'")
                if d.startswith("/"):
                    if d in self._ol_dirs:
                        self._ol_dirs.remove(d)
                    self._ol_dirs.insert(0, d)
                    del self._ol_dirs[6:]
        cost = self._ctl.call_cost(float(usage.prompt_tokens or 0), float(usage.cache_tokens or 0),
                                   float(usage.completion_tokens or 0))
        return self._ol_rr.close_turn(cost)

    @staticmethod
    def _ol_env_run(session):
        """`run(cmd) -> (return code, stdout)` in the task environment of this trial."""
        async def run(cmd: str) -> tuple[int, str]:
            res = await session.environment.exec(command=cmd, timeout_sec=20)
            return res.return_code, res.stdout or ""
        return run

    async def _ol_carry_block(self, session) -> tuple[str, list[str]]:
        """File Carry-over: current contents of the files the agent keeps re-reading (optlimit/carry.py)."""
        c = self._ol_cfg
        return await disk_block(self._ol_rr, self._ol_env_run(session), self._ol_dirs, c.carry_files,
                                c.carry_item_tokens, order=c.carry_order)
