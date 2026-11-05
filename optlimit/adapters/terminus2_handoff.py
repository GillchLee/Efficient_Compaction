"""Hand-off prompt variants for terminus-2 (the `handoff:` block of OptlimitTerminus2).

Mixin over harbor's Terminus2: every method here overrides a prompt builder that
patches/02-terminus2-prompts-and-short-summary.patch factored out of terminus_2.py
(_summary_prompt / _question_prompt / _answer_prompt) or replaces _summarize for the
single-call hand-off. With an empty `handoff` block every method falls through to the
stock prompt, so the default behaviour is unchanged.

    handoff:
      summary_words: 2600   # "approximately N words" in the summary prompt
      min_questions: 12     # "at least N questions" (stock: five)
      answer_words: 2000    # "approximately N words in total" in the answer prompt
      mode: single          # one structured hand-off call instead of the three stock calls
"""
from __future__ import annotations


class HandoffPromptsMixin:
    HANDOFF_KEYS = ("mode", "summary_words", "min_questions", "answer_words")

    @classmethod
    def validate_handoff(cls, handoff: dict | None) -> dict:
        h = dict(handoff or {})
        unknown = set(h) - set(cls.HANDOFF_KEYS)
        if unknown:
            raise ValueError(f"handoff: unknown keys {sorted(unknown)} (allowed: {cls.HANDOFF_KEYS})")
        if h.get("mode", "stock") not in ("stock", "single"):
            raise ValueError("handoff.mode must be 'stock' or 'single'")
        return h

    def _summary_prompt(self, original_instruction: str) -> str:
        base = super()._summary_prompt(original_instruction)
        n = self._handoff.get("summary_words")
        if not n:
            return base
        return base + (
            f"\n\nWrite approximately {int(n)} words. Be concrete rather than general: quote exact file"
            " paths, commands, code snippets, parameter values, numeric results, and error messages"
            " verbatim wherever they matter for continuing the work."
        )

    def _question_prompt(self, original_instruction: str, summary: str, current_screen: str) -> str:
        base = super()._question_prompt(original_instruction, summary, current_screen)
        n = self._handoff.get("min_questions")
        if not n:
            return base
        return base.replace(
            "asking several questions (at least five, more if necessary)",
            f"asking several questions (at least {int(n)}, more if necessary)",
        ) + (
            "\nMake each question specific (exact files, commands, values, failing checks) so that"
            " the answers can be acted on directly."
        )

    # ---- single-call structured hand-off (handoff.mode: single)
    def _single_summary_prompt(self, original_instruction: str) -> str:
        n = self._handoff.get("summary_words") or 800
        return f"""You are about to hand off your work to another AI agent that will NOT see this
conversation. Write the hand-off note it needs to continue without redoing anything.

Original Task: {original_instruction}

Write approximately {int(n)} words, as these sections:
1. **Status** - what is done, what is not, and the exact last thing you were doing.
2. **Files and state** - every file you created or changed (exact paths) and what is in them now;
   environment facts (installed tools, versions, data locations, credentials or ports) verbatim.
3. **Commands that matter** - the exact commands to run tests / feedback / build, and their last
   key results (numbers, pass/fail, error messages verbatim). Do not list commands twice.
4. **Open problems** - what failed, what you ruled out, and the hypotheses still open.
5. **Next steps** - the concrete next actions, in order.

Quote exact paths, commands, values and error text; do not summarize where the exact text matters."""

    def _single_handoff_prompt(self, original_instruction: str, summary: str, current_screen: str) -> str:
        return f"""You are picking up work from a previous AI agent on this task:

**Original Task:** {original_instruction}

**Hand-off note from the previous agent:**
{summary}

**Current Terminal Screen:**
{current_screen}

Continue working on this task from where the previous agent left off. Do not redo work the
note says is done; verify only what the next step needs. Please follow the spec to interact
with the terminal."""

    async def _summarize(self, chat, original_instruction, session):
        if self._handoff.get("mode", "stock") != "single":
            return await super()._summarize(chat, original_instruction, session)
        if len(chat.messages) == 0:
            return original_instruction, None
        self._summarization_count += 1
        steps_to_include = 1 + (len(chat.messages) - 1) // 2
        summary_steps, _ = self._prepare_copied_trajectory_steps(steps_to_include)
        summary_response, ref = await self._run_subagent(
            prompt=self._single_summary_prompt(original_instruction),
            message_history=chat.messages,
            steps=summary_steps,
            session_id=f"{self._session_id}-summarization-{self._summarization_count}-summary",
            agent_name="terminus-2-summarization-summary",
            filename_suffix="summary",
            summary_text=f"Context summarization {self._summarization_count}: single structured hand-off",
            subagent_name_for_logging="single hand-off LLM call",
        )
        current_screen = await session.capture_pane(capture_entire=False)
        chat._messages = [chat.messages[0]]
        chat.reset_response_chain()
        handoff_prompt = self._single_handoff_prompt(original_instruction, summary_response.content, current_screen)
        return handoff_prompt, [ref]

    def _answer_prompt(self, model_questions: str) -> str:
        n = self._handoff.get("answer_words")
        if not n:
            return super()._answer_prompt(model_questions)
        return (
            "The next agent has a few questions for you, please answer each of them one by one in detail"
            f" (approximately {int(n)} words in total). Quote exact commands, file paths, code and values"
            " from the conversation; do not summarize where the exact text matters:\n\n" + model_questions
        )

