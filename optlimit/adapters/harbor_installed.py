"""Harbor adapters for harnesses that compact on their own (Claude Code, Codex).

terminus-2 exposes its summarization to Python, so optlimit.adapters.harbor_terminus2 calls it
directly. Claude Code and Codex run as CLIs inside the task container and compact internally;
optlimit reaches them two ways (docs/other_harnesses.md):

* static   - set the harness's own threshold at launch: Claude Code
             CLAUDE_CODE_AUTO_COMPACT_WINDOW x CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, Codex
             model_auto_compact_token_limit. A fixed L, or a formula L* computed before the run.
* proxy    - optlimit.serve sits between the CLI and the API, runs the controller on every
             call's usage (formula or balance) and makes the CLI compact at the chosen moment
             by rewriting the reported usage (trigger=usage) or answering with a
             context-length error (trigger=error). The static threshold is then set between the
             proxy's low cap and high signal so the CLI only compacts on the signal.

Both classes below add only what the stock Harbor agents lack:

* `compaction:` kwargs that set the native threshold (static mode, or the band for proxy mode);
* resume_after_verifier_rejection(), so HB_CONTINUE_MODE=same_conversation continues the
  same CLI conversation after a verifier rejection, like terminus-2 in the other experiments
  (Claude Code: `claude --continue`; Codex: `codex exec resume --last`).

    agents:
      - import_path: optlimit.adapters.harbor_installed:OptlimitClaudeCode
        model_name: anthropic/claude-sonnet-5-5
        kwargs:
          compaction: {window: 100000, limit: 87000}   # compacts at ~87K (static)
"""
from __future__ import annotations

import math
import shlex
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.codex import Codex

# Claude Code: CLAUDE_CODE_AUTO_COMPACT_WINDOW accepts 100K..1M; CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
# (1..100) can only lower the trigger, as a percentage of that window.
CC_WINDOW_MIN, CC_WINDOW_MAX = 100_000, 1_000_000


def claude_code_compaction_env(limit: int | None = None, window: int | None = None) -> dict[str, str]:
    """Env vars that make Claude Code auto-compact at ~`limit` prompt tokens.

    The window is clamped to Claude Code's accepted range; the percentage is rounded up so the
    effective trigger is the smallest settable value >= limit (1% of the window resolution).
    """
    env: dict[str, str] = {}
    if limit is None and window is None:
        return env
    if window is None:
        window = max(CC_WINDOW_MIN, min(CC_WINDOW_MAX, int(limit)))
    window = max(CC_WINDOW_MIN, min(CC_WINDOW_MAX, int(window)))
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
    if limit is not None and limit < window:
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(max(1, min(100, math.ceil(100 * limit / window))))
    return env


def _options_with_compaction(base):
    """Harbor >= 0.20 validates agent kwargs against a pydantic ``options_model`` (extra = forbid),
    also in a preflight before the constructor runs, so the ``compaction`` kwarg has to be declared
    there. Older Harbor (the LHTB fork) has no options_model and takes any kwarg."""
    model = getattr(base, "options_model", None)
    if model is None:
        return None
    return type(f"Optlimit{model.__name__}", (model,),
                {"__annotations__": {"compaction": dict | None}, "compaction": None, "__module__": __name__})


def _mark_completed(context) -> None:
    """A CLI run that returned normally is the agent declaring the task done. Harbor's
    continue-until-timeout loop (same_conversation) runs the interim verifier - and resumes on a
    rejection - only for termination_reason == "confirmed_task_complete", which terminus-2 sets
    itself; the installed agents set nothing, so record it here."""
    if context is None:
        return
    if context.metadata is None:
        context.metadata = {}
    context.metadata.setdefault("termination_reason", "confirmed_task_complete")


class OptlimitClaudeCode(ClaudeCode):
    """claude-code with a settable compaction threshold and same-conversation resume.

    kwargs:
      compaction: {limit: N, window: W}   native auto-compact trigger (see claude_code_compaction_env);
                                          omit for Claude Code's default (~model window)
    Other kwargs are the stock claude-code ones (extra_env, max_turns, ...).
    """

    if getattr(ClaudeCode, "options_model", None) is not None:
        options_model = _options_with_compaction(ClaudeCode)

    def __init__(self, logs_dir: Path, *args, compaction: dict | None = None, **kwargs):
        compaction = dict(compaction or {})
        unknown = set(compaction) - {"limit", "window"}
        if unknown:
            raise ValueError(f"compaction: unknown keys {sorted(unknown)} (known: limit, window)")
        env = claude_code_compaction_env(compaction.get("limit"), compaction.get("window"))
        extra_env = {**env, **dict(kwargs.pop("extra_env", None) or {})}
        super().__init__(logs_dir, *args, extra_env=extra_env, **kwargs)
        self._compaction_env = env
        self._environment = None
        self._resume = False

    def build_cli_flags(self) -> str:
        flags = super().build_cli_flags()
        return f"{flags} --continue".strip() if self._resume else flags

    async def run(self, instruction, environment, context) -> None:
        self._environment = environment
        await super().run(instruction, environment, context)
        _mark_completed(context)

    async def resume_after_verifier_rejection(self, user_prompt: str, context) -> None:
        """Continue the most recent conversation (`claude --continue --print <prompt>`)."""
        if self._environment is None:
            raise RuntimeError("Cannot resume before the initial agent run")
        self._resume = True
        try:
            await super().run(user_prompt, self._environment, context)
        finally:
            self._resume = False
        _mark_completed(context)


# Codex (codex-rs core, 2026-11): auto-compacts when the last response's total_tokens (+ local
# estimate of items since) >= min(model_auto_compact_token_limit, 90% of the window); 95% of the
# window is a hard cap. Setting model_context_window is known to break auto-compaction
# (openai/codex #16033 / #16068), so it is only passed when asked for explicitly.
CODEX_SCOPES = ("total", "body_after_prefix")


def codex_compaction_flags(limit: int | None = None, scope: str | None = None,
                           context_window: int | None = None) -> list[str]:
    """`-c key=value` overrides for `codex exec` that set the native compaction trigger."""
    flags: list[str] = []
    if limit is not None:
        flags.append(f"-c model_auto_compact_token_limit={int(limit)}")
    if scope is not None:
        if scope not in CODEX_SCOPES:
            raise ValueError(f"compaction.scope must be one of {CODEX_SCOPES}")
        flags.append("-c " + shlex.quote(f'model_auto_compact_token_limit_scope="{scope}"'))
    if context_window is not None:
        flags.append(f"-c model_context_window={int(context_window)}")
    return flags


class OptlimitCodex(Codex):
    """codex with a settable compaction trigger and same-conversation resume.

    kwargs:
      compaction: {limit: N, scope: total|body_after_prefix, context_window: W}
    Resume runs `codex exec <flags> resume --last -- <prompt>` after restoring the session
    rollouts that the stock agent copies to /logs/agent/sessions and then deletes from CODEX_HOME.
    """

    if getattr(Codex, "options_model", None) is not None:
        options_model = _options_with_compaction(Codex)

    def __init__(self, logs_dir: Path, *args, compaction: dict | None = None, **kwargs):
        compaction = dict(compaction or {})
        unknown = set(compaction) - {"limit", "scope", "context_window"}
        if unknown:
            raise ValueError(f"compaction: unknown keys {sorted(unknown)} (known: limit, scope, context_window)")
        self._compaction_flags = codex_compaction_flags(compaction.get("limit"), compaction.get("scope"),
                                                        compaction.get("context_window"))
        super().__init__(logs_dir, *args, **kwargs)
        self._environment = None
        self._resume = False

    def build_cli_flags(self) -> str:
        return " ".join([f for f in [super().build_cli_flags(), *self._compaction_flags] if f])

    async def exec_as_agent(self, environment, command: str, env=None, cwd=None, timeout_sec=None):
        if self._resume and "codex exec " in command and " -- " in command:
            restore = ('if [ -d /logs/agent/sessions ]; then mkdir -p "$CODEX_HOME" && '
                       'cp -R /logs/agent/sessions "$CODEX_HOME/sessions"; fi; ')
            command = restore + command.replace(" -- ", " resume --last -- ", 1)
        return await super().exec_as_agent(environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec)

    async def run(self, instruction, environment, context) -> None:
        self._environment = environment
        await super().run(instruction, environment, context)
        _mark_completed(context)

    async def resume_after_verifier_rejection(self, user_prompt: str, context) -> None:
        """Continue the most recent Codex session with the rejection turn."""
        if self._environment is None:
            raise RuntimeError("Cannot resume before the initial agent run")
        self._resume = True
        try:
            await super().run(user_prompt, self._environment, context)
        finally:
            self._resume = False
        _mark_completed(context)
