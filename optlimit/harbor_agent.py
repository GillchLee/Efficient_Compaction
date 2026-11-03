"""terminus-2 whose compaction *timing* is decided by optlimit, in-process (no proxy).

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
call (`chat.last_usage`, added by patches/harbor-token-and-summary-tracking.patch). When the
controller says "compact now" it runs terminus-2's own `_summarize()` - the same three-call
summarization the stock agent uses - so only the moment of compaction changes. Otherwise the
stock threshold check still runs (safety net). Every decision is appended to
`<agent logs>/optlimit_decisions.jsonl`; analysis/token_turns.py joins it to turns.csv.

`mode: off` never compacts on its own (stock behaviour) but still logs the prompts, so the
same agent class can run the native baseline.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from harbor.agents.terminus_2.terminus_2 import Terminus2

from .controller import LimitController, config_from_spec

class OptlimitTerminus2(Terminus2):
    def __init__(self, logs_dir: Path, model_name: str | None = None, optlimit: dict | None = None, **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self._ol_cfg = config_from_spec(optlimit)
        self._ctl = LimitController(self._ol_cfg)
        self._ol_key = "main"
        self._ol_seen = None          # last UsageInfo object the controller has consumed
        self._ol_boundary = False     # a summarization happened: the next prompt starts a window
        self._ol_path = Path(logs_dir) / "optlimit_decisions.jsonl"
        self.logger.info(f"optlimit controller: {self._ol_cfg}")

    # ------------------------------------------------------------------ logging
    def _ol_log(self, rec: dict) -> None:
        try:
            self._ol_path.parent.mkdir(parents=True, exist_ok=True)
            with self._ol_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:  # never break the run because of the log
            self.logger.warning(f"optlimit: cannot write {self._ol_path}: {e}")

    # ------------------------------------------------------------------ the hook
    async def _check_proactive_summarization(self, chat, original_instruction, session):
        usage = getattr(chat, "last_usage", None)
        if usage is None or usage is self._ol_seen:
            # first episode of a chat, or the previous episode made no new main call
            return await super()._check_proactive_summarization(chat, original_instruction, session)
        self._ol_seen = usage

        d = self._ctl.on_prompt(self._ol_key, "main", float(usage.prompt_tokens),
                                cached=float(usage.cache_tokens or 0), window_start_hint=self._ol_boundary)
        self._ol_boundary = False
        rec = {"ts": time.time(), "prompt": usage.prompt_tokens, "cached": usage.cache_tokens,
               **d.as_dict(), "action": None}

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
        self._ol_log(rec)
        return result
