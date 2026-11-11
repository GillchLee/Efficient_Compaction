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

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shlex
import subprocess
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



# ---------------------------------------------------------------------------------------------
# Robust in-container install (SWE-Marathon chain 2026-11-12: 13 of 18 Claude Code trials died
# at setup). Two independent infra failures, both outside the model/policy under test:
#   * the task container shares the egress sidecar's network namespace and apt ran before the
#     sidecar's DNS was up ("Could not resolve archive.ubuntu.com" within 2 min of trial start);
#   * every trial re-downloads the 342 MB Claude Code binary from downloads.claude.ai, which at
#     130-700 KB/s per container exceeds the 1200 s setup timeout.
# Fix: wait for DNS (bounded) + retry the package step, and download the binary ONCE into a host
# cache (sha256-checked against the release manifest), then `docker compose cp` it into the
# container and run its own `claude install`. OPTLIMIT_CLAUDE_CACHE=0 restores the stock
# bootstrap path; OPTLIMIT_CLAUDE_CACHE=<dir> moves the cache (default ~/.cache/optlimit/claude-code).
# ---------------------------------------------------------------------------------------------
CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"
CLAUDE_PLATFORM = "linux-x64"
_DNS_WAIT_CMD = (
    "command -v getent >/dev/null 2>&1 || exit 0; "
    "for i in $(seq 1 45); do getent hosts archive.ubuntu.com >/dev/null 2>&1 || "
    "getent hosts deb.debian.org >/dev/null 2>&1 || getent hosts downloads.claude.ai >/dev/null 2>&1 && exit 0; "
    "sleep 2; done; echo 'optlimit: container DNS still failing after 90 s' >&2; exit 0"
)
_log = logging.getLogger(__name__)


def claude_cache_dir() -> Path | None:
    v = os.environ.get("OPTLIMIT_CLAUDE_CACHE", "")
    if v.lower() in ("0", "off", "no", "false"):
        return None
    return Path(v) if v else Path.home() / ".cache" / "optlimit" / "claude-code"


def _curl(url: str, out: Path | None = None, timeout: int = 20) -> str:
    args = ["curl", "-4", "-fsSL", "-m", str(timeout), url] + (["-C", "-", "-o", str(out)] if out else [])
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 30)
    if r.returncode != 0:
        raise RuntimeError(f"curl {url}: rc={r.returncode} {r.stderr.strip()[:200]}")
    return r.stdout


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cached_claude_binary(version: str | None = None, cache: Path | None = None,
                         download_timeout: int = 900) -> Path | None:
    """Path of the sha256-verified Claude Code binary in the host cache, downloading it once.

    version: an explicit release (``2.1.322``); None/stable/latest resolve the release's ``latest``
    pointer, falling back to the newest cached release when the pointer cannot be fetched.
    Returns None when the cache is disabled or nothing could be obtained (caller falls back to
    the stock in-container bootstrap).
    """
    cache = cache or claude_cache_dir()
    if cache is None:
        return None
    cache.mkdir(parents=True, exist_ok=True)
    ver = version if version and re.match(r"^\d+\.\d+\.\d+", version) else None
    if ver is None:
        # No pin: the newest cached release, so every trial of a sweep runs the same binary and
        # nothing touches the network (refresh = delete the cache dir or pin a version).
        have = sorted((d for d in cache.iterdir() if (d / "claude").is_file()),
                      key=lambda d: [int(x) for x in re.findall(r"\d+", d.name)[:3]])
        if have:
            return have[-1] / "claude"
        try:
            ver = _curl(f"{CLAUDE_RELEASES}/latest").strip()
        except Exception as e:  # noqa: BLE001
            _log.warning("claude cache: cannot resolve latest (%s) and nothing cached", e)
            return None
    d = cache / ver
    final = d / "claude"
    if final.is_file():
        return final
    d.mkdir(parents=True, exist_ok=True)
    try:
        manifest = d / "manifest.json"
        if not manifest.is_file():
            manifest.write_text(_curl(f"{CLAUDE_RELEASES}/{ver}/manifest.json"))
        plat = json.loads(manifest.read_text())["platforms"][CLAUDE_PLATFORM]
        part = d / "claude.part"
        _curl(f"{CLAUDE_RELEASES}/{ver}/{CLAUDE_PLATFORM}/claude", part, timeout=download_timeout)
        if _sha256(part) != plat["checksum"]:
            part.unlink(missing_ok=True)
            raise RuntimeError("checksum mismatch")
        part.chmod(0o755)
        part.rename(final)
        return final
    except Exception as e:  # noqa: BLE001
        _log.warning("claude cache: download of %s failed (%s)", ver, e)
        return None


class _RobustInstall:
    """Mixin: wait for the sidecar DNS and retry the system-package step (see module comment)."""

    _INSTALL_RETRIES = 3

    async def _wait_for_dns(self, environment) -> None:
        try:
            await self.exec_as_root(environment, command=_DNS_WAIT_CMD, timeout_sec=120)
        except Exception as e:  # noqa: BLE001
            self.logger.warning("DNS wait failed (%s); continuing", e)

    async def ensure_system_dependencies(self, environment, dependencies) -> None:
        last: Exception | None = None
        for attempt in range(1, self._INSTALL_RETRIES + 1):
            await self._wait_for_dns(environment)
            try:
                await super().ensure_system_dependencies(environment, dependencies)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                self.logger.warning("system package install attempt %d/%d failed: %s",
                                    attempt, self._INSTALL_RETRIES, str(e)[:300])
                await asyncio.sleep(15 * attempt)
        assert last is not None
        raise last

class OptlimitClaudeCode(_RobustInstall, ClaudeCode):
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

    async def install(self, environment) -> None:
        """Stock install, but with the binary from the host cache when available (module comment)."""
        if await self._installed_claude_satisfies_version(environment):
            return
        await self.ensure_system_dependencies(environment, ("curl", "bash", "nodejs", "npm", "procps"))
        binary = await asyncio.to_thread(cached_claude_binary, self._version)
        if binary is None:
            self.logger.warning("claude cache unavailable; falling back to the in-container bootstrap")
            await super().install(environment)
            return
        ver = binary.parent.name
        self.logger.info("installing Claude Code %s from host cache %s", ver, binary)
        await environment.upload_file(binary, "/tmp/optlimit-claude")
        # Same layout as the official installer (~/.local/share/claude/versions/<ver> + symlink
        # ~/.local/bin/claude), written by hand: the binary's own `install` re-downloads `latest`.
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; V=" + shlex.quote(ver) + "; "
                'mkdir -p "$HOME/.local/bin" "$HOME/.local/share/claude/versions" && '
                'cp /tmp/optlimit-claude "$HOME/.local/share/claude/versions/$V" && '
                'chmod 755 "$HOME/.local/share/claude/versions/$V" && '
                'ln -sfn "$HOME/.local/share/claude/versions/$V" "$HOME/.local/bin/claude" && '
                "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc && "
                'export PATH="$HOME/.local/bin:$PATH" && claude --version'
            ),
        )
        await self.exec_as_root(environment, command="rm -f /tmp/optlimit-claude")

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


class OptlimitCodex(_RobustInstall, Codex):
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
