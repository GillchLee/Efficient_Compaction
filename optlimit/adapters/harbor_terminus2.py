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
call (`chat.last_usage`, added by patches/01-usage-tracking.patch). When the
controller says "compact now" it runs terminus-2's own `_summarize()` - the same three-call
summarization the stock agent uses - so only the moment of compaction changes. Otherwise the
stock threshold check still runs (safety net). Every decision is appended to
`<agent logs>/optlimit_decisions.jsonl`; analysis/token_turns.py joins it to turns.csv.

`mode: off` never compacts on its own (stock behaviour) but still logs the prompts, so the
same agent class can run the native baseline.

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
from .terminus2_handoff import HandoffPromptsMixin

class OptlimitTerminus2(HandoffPromptsMixin, Terminus2):

    def __init__(self, logs_dir: Path, model_name: str | None = None, optlimit: dict | None = None,
                 handoff: dict | None = None, **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        optlimit = dict(optlimit or {})
        self._ol_prices_row = optlimit.get("prices") if optlimit.get("prices_reload") else None
        self._ol_prices_mtime = self._prices_mtime() if self._ol_prices_row else None
        self._ol_cfg = config_from_spec(optlimit)
        self._handoff = self.validate_handoff(handoff)
        self._ctl = LimitController(self._ol_cfg)
        self._ol_key = "main"
        self._ol_seen = None          # last UsageInfo object the controller has consumed
        self._ol_boundary = False     # a summarization happened: the next prompt starts a window
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
    async def _check_proactive_summarization(self, chat, original_instruction, session):
        usage = getattr(chat, "last_usage", None)
        if usage is None or usage is self._ol_seen:
            # first episode of a chat, or the previous episode made no new main call
            return await super()._check_proactive_summarization(chat, original_instruction, session)
        self._ol_seen = usage
        reloaded = self._maybe_reload_prices()

        d = self._ctl.on_prompt(self._ol_key, "main", float(usage.prompt_tokens),
                                cached=float(usage.cache_tokens or 0), window_start_hint=self._ol_boundary)
        self._ol_boundary = False
        rec = {"ts": time.time(), "prompt": usage.prompt_tokens, "cached": usage.cache_tokens,
               **d.as_dict(), "action": None}
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
        self._ol_log(rec)
        return result
