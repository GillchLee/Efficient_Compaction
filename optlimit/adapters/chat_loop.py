"""Reference adapter: a plain chat loop (any OpenAI-compatible or Anthropic client) with
optlimit deciding when to compact.

This is the whole harness contract, in one place (docs/adapter_guide.md):

  1. after every model call, hand the API-reported usage of that call to the controller
     (`on_prompt`), before the next call is built;
  2. when the decision says `compact_now`, run the harness's own summarization and hand the
     usage of each summarization call to `note_summary_usage`;
  3. tell the controller the next prompt starts a new window (`note_kind(.., "summary")` /
     `window_start_hint=True`);
  4. give it the price ratios alpha = write/read, beta = output/read (`load_prices` or
     `update_prices`).

Everything else (how messages are stored, how the summary is produced, tools, retries) stays
in the harness. The loop below is generic: `call(messages) -> (text, usage)` and
`summarize(messages) -> (new_messages, [usage, ...])` are injected, so it also serves as the
offline test of the contract (tests/test_chat_loop.py).

    from optlimit.controller import config_from_spec
    from optlimit.adapters.chat_loop import CompactingChatLoop
    loop = CompactingChatLoop(config_from_spec({"mode": "formula", "prior": "...", "prices": "..."}),
                              call=my_call, summarize=my_summarize)
    reply = loop.step(user_text)      # repeatedly
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..controller import ControllerConfig, LimitController
from ..usage import normalize_usage

Call = Callable[[list[dict]], tuple[str, object]]
Summarize = Callable[[list[dict]], tuple[list[dict], list[object]]]


class CompactingChatLoop:
    def __init__(self, cfg: ControllerConfig, call: Call, summarize: Summarize,
                 messages: list[dict] | None = None, log_path: str | Path | None = None):
        self.ctl = LimitController(cfg)
        self.call, self.summarize = call, summarize
        self.messages: list[dict] = list(messages or [])
        self.key = "main"
        self.log_path = Path(log_path) if log_path else None
        self.decisions: list[dict] = []
        self._boundary = False
        self.n_compactions = 0

    def _log(self, rec: dict) -> None:
        self.decisions.append(rec)
        if self.log_path:
            with self.log_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    def step(self, user_text: str) -> str:
        """One user turn: maybe compact first (decision from the previous call's usage), then call."""
        self.messages.append({"role": "user", "content": user_text})
        text, raw = self.call(self.messages)
        self.messages.append({"role": "assistant", "content": text})
        u = normalize_usage(raw)
        d = self.ctl.on_prompt(self.key, "main", u.prompt, cached=u.cached, window_start_hint=self._boundary)
        self._boundary = False
        rec = {"prompt": u.prompt, "cached": u.cached, **d.as_dict(), "action": None}
        if d.compact_now:
            new_messages, usages = self.summarize(self.messages)
            for su in usages:
                s = normalize_usage(su)
                self.ctl.note_summary_usage(self.key, s.prompt, cached=s.cached, completion=s.completion)
            self.messages = list(new_messages)
            self.ctl.note_kind(self.key, "summary")
            self._boundary = True
            self.n_compactions += 1
            rec["action"] = "summarize"
        self._log(rec)
        return text
