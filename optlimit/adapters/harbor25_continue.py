"""Continue-until-timeout on stock Harbor (>= 0.20): LHTB's trial loop for the PyPI package.

LHTB tasks set `[agent] continue_until_timeout = true`: when the agent stops, the verifier runs; if it
does not pass, the agent goes on in the same conversation with a binary rejection, until the verifier
passes or the agent budget is used up.  Stock Harbor ignores the flag (one agent run, one verifier
run), so LHTB there ends at the agent's first "done".  LHTB ships the loop in its own Harbor fork
(0.7) and as a replacement for `harbor/trial/single_step.py` written against 0.20
(LHTB/harbor/patches/single_step.py.harbor-0.20.0).  This module is that loop for the Harbor version
that is installed, without replacing a file of the package:

* `install()` wraps `SingleStepTrial._run_agent`.  Each phase goes through Harbor's own
  `_run_agent_phase` (network policy, logging, trajectory sync of that version); a continuation uses
  the agent's native `resume()` (Codex: `codex exec resume --last`, Claude Code: `--continue`).
* Taken from LHTB's replacement file: the loop, the grader isolation between phases (the agent's
  tmux process tree is frozen while the verifier runs; `/tests` is removed and the verifier's log
  directory is moved to the host-side `interim_verifier_artifacts/` before the agent goes on) and
  the per-phase metrics merge.  The rejection text is the fork's same-conversation one, so a run
  here and a run on the fork give the agent the same words.
* Not taken: the 30-minute checkpoint that stops a running agent to verify (`LHTB_CHECKPOINT_INTERVAL_SEC`
  of the replacement file; the fork has none).  Set `HB_CHECKPOINT_INTERVAL_SEC` to get it.

It is active only when `HB_CONTINUE_MODE=same_conversation` (make_config.py writes that into LHTB
configs; scripts/run_job.sh exports it) and only for tasks that set the flag; everything else runs
the stock code path.  optlimit/adapters/harbor_installed.py calls `install_from_env()` on import.
Each interim verification is appended to `<trial>/interim_verifier.jsonl` (same fields as the fork
patch 03).  Shared verifier environments are the tested case (all LHTB 2.0 reconstruction tasks).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import tomllib
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# STOP / CONT the agent's tmux process tree (LHTB replacement file).  Walks /proc with sed: ps / pgrep
# are missing in slim images.  A CLI agent (Codex, Claude Code) has exited when the verifier runs, so
# for it this finds nothing and returns 0.
_AGENT_TREE_SIGNAL_CMD = r"""
roots=""
for d in /proc/[0-9]*; do
  [ -r "$d/status" ] || continue
  name=$(sed -n 's/^Name:[[:space:]]*//p' "$d/status" 2>/dev/null)
  case "$name" in tmux*) roots="$roots ${d#/proc/}";; esac
done
[ -n "$roots" ] || exit 0
marked="$roots"
changed=1
while [ "$changed" -eq 1 ]; do
  changed=0
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    case " $marked " in *" $pid "*) continue;; esac
    ppid=$(sed -n 's/^PPid:[[:space:]]*//p' "$d/status" 2>/dev/null)
    [ -n "$ppid" ] || continue
    case " $marked " in
      *" $ppid "*) marked="$marked $pid"; changed=1;;
    esac
  done
done
for pid in $marked; do
  case " $roots " in *" $pid "*) continue;; esac
  kill -__SIGNAL__ "$pid" 2>/dev/null || true
done
exit 0
"""


def enabled_by_env() -> bool:
    return os.environ.get("HB_CONTINUE_MODE", "").strip().lower() == "same_conversation"


def task_wants_loop(task) -> bool:
    """`[agent] continue_until_timeout` of the task (stock Harbor's AgentConfig drops the key)."""
    configured = getattr(task.config.agent, "continue_until_timeout", None)
    if configured is not None:
        return bool(configured)
    try:
        raw = tomllib.loads(task.paths.config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return bool(raw.get("agent", {}).get("continue_until_timeout", False))


def rejection_text(*, phase: int, remaining_sec: int) -> str:
    """The fork's same-conversation rejection: binary, nothing derived from the verifier."""
    return (
        "Your submitted solution did not pass verification. No verifier details, "
        "test output, or hidden requirements are available. Continue working from "
        "the current terminal and conversation state. Re-read the original "
        "requirements, inspect your implementation, and run your own focused "
        f"checks before submitting again. This is continuation {phase}; "
        f"approximately {remaining_sec} seconds remain."
    )


def reward_of(result) -> float | None:
    rewards = getattr(result, "rewards", None)
    if isinstance(rewards, dict):
        r = rewards.get("reward")
        if isinstance(r, (int, float)):
            return float(r)
    r = getattr(result, "reward", None)
    return float(r) if isinstance(r, (int, float)) else None


def verifier_passed(result) -> bool:
    r = reward_of(result)
    return r is not None and r >= 1.0


def merge_contexts(acc, phase):
    """Sum the token / cost counters of the phases; later metadata wins (LHTB replacement file)."""
    if acc is None:
        return phase.model_copy(deep=True)
    if phase is None:
        return acc

    def add(a, b):
        return None if a is None and b is None else (a or 0) + (b or 0)

    meta: dict[str, Any] = dict(acc.metadata or {})
    for k, v in (phase.metadata or {}).items():
        if k == "n_episodes" and isinstance(v, int) and isinstance(meta.get(k), int):
            meta[k] += v
        else:
            meta[k] = v
    update = {"metadata": meta or None}
    for f in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd"):
        if hasattr(acc, f):
            update[f] = add(getattr(acc, f), getattr(phase, f, None))
    if hasattr(acc, "rollout_details"):
        update["rollout_details"] = (list(acc.rollout_details or []) + list(phase.rollout_details or [])) or None
    return acc.model_copy(update=update)


async def _signal_agent_tree(trial, signal_name: str) -> bool:
    if "linux" not in str(trial.task.config.environment.os).lower():
        return True
    try:
        res = await trial.agent_environment.exec(_AGENT_TREE_SIGNAL_CMD.replace("__SIGNAL__", signal_name),
                                                 timeout_sec=60)
        rc = getattr(res, "return_code", 0)
        if isinstance(rc, int) and rc != 0:
            trial.logger.warning("agent process-tree SIG%s exited with code %s", signal_name, rc)
            return False
        return True
    except Exception as exc:
        trial.logger.warning("could not send SIG%s to the agent process tree: %s", signal_name, exc)
        return False


async def _hide_grader(trial, submission: int) -> None:
    """Before the agent goes on: no hidden tests and no grader output where it can read them."""
    from harbor.models.trial.paths import EnvironmentPaths

    env_paths = EnvironmentPaths.for_os(trial.task.config.environment.os)
    remove, create = [env_paths.tests_dir], []
    mounted = bool(getattr(getattr(trial.agent_environment, "capabilities", None), "mounted", False))
    if mounted:
        # /logs/verifier is the host's <trial>/verifier: keep a copy outside the mount, then empty it
        live = trial.paths.verifier_dir
        try:
            if live.exists() and any(live.iterdir()):
                snap = trial.paths.trial_dir / "interim_verifier_artifacts" / f"phase-{submission:04d}"
                snap.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(live, snap, dirs_exist_ok=True)
        except OSError as exc:
            trial.logger.warning("could not snapshot the interim verifier output: %s", exc)
        try:
            await trial.agent_environment.empty_dirs([env_paths.verifier_dir])
        except Exception as exc:
            raise RuntimeError("refusing to resume the agent: the verifier output could not be hidden") from exc
    else:
        remove.append(env_paths.verifier_dir)
        create.append(env_paths.verifier_dir)
    try:
        await trial.agent_environment.reset_dirs(remove_dirs=remove, create_dirs=create)
    except Exception as exc:
        raise RuntimeError("refusing to resume the agent: the hidden tests could not be removed") from exc


async def _interim_verify(trial):
    from harbor.models.task.verifier_mode import VerifierEnvironmentMode, resolve_task_verifier_mode

    user = trial.task.config.verifier.user
    if resolve_task_verifier_mode(trial.task.config) == VerifierEnvironmentMode.SEPARATE:
        trial._are_artifacts_collected = False          # the current submission, not the first one
        await trial._collect_artifacts(stop_main_before_sidecars=False)
        trial._are_artifacts_collected = False
        return await trial._run_separate_verifier(key="interim", timeout_sec=trial._verifier_timeout_sec,
                                                  artifacts_dir=trial.paths.artifacts_dir, user=user)
    return await trial._run_shared_verifier(timeout_sec=trial._verifier_timeout_sec, user=user)


def _record(trial, **row) -> None:
    try:
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), **row}
        with open(trial.paths.trial_dir / "interim_verifier.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        trial.logger.warning("could not record the interim verifier result: %s", exc)


async def run_agent_until_timeout(trial) -> None:
    """Agent -> verifier -> (rejected) resume the same conversation -> ... until pass or budget end."""
    from harbor.trial.errors import AgentTimeoutError, VerifierTimeoutError

    budget = float(trial._agent_timeout_sec)
    interval = float(os.environ.get("HB_CHECKPOINT_INTERVAL_SEC") or 0) or None
    user = trial.task.config.agent.user
    started = time.monotonic()
    deadline = started + budget
    first_start = None
    phase = 0
    checkpoints: list[dict] = []
    aggregated = None
    instruction = trial.task.instruction
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            slot = min(remaining, interval) if interval else remaining
            cut_short = False
            try:
                await trial._run_agent_phase(target=trial.result, instruction=instruction, timeout_sec=slot,
                                             user=user, resume=phase > 0)
            except NotImplementedError:
                trial.logger.warning("agent cannot resume: keeping the result of phase %s", phase - 1)
                break
            except AgentTimeoutError:
                if time.monotonic() >= deadline - 1:
                    aggregated = merge_contexts(aggregated, trial.result.agent_result)
                    raise AgentTimeoutError(f"Agent execution timed out after {budget:.0f} seconds") from None
                cut_short = True                         # checkpoint interval: verify, then go on
            finally:
                ex = trial.result.agent_execution
                if ex is not None:
                    first_start = first_start or ex.started_at
                    ex.started_at = first_start
            ctx = trial.result.agent_result
            if ctx is not None:
                ctx.metadata = {**(ctx.metadata or {}), "continue_until_timeout_phase": phase}
            aggregated = merge_contexts(aggregated, ctx)
            trial.result.agent_result = aggregated

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not await _signal_agent_tree(trial, "STOP"):
                raise RuntimeError("refusing to run an interim verifier: the agent process tree could not be frozen")
            interim, status = None, "ok"
            try:
                interim = await _interim_verify(trial)
            except (asyncio.TimeoutError, VerifierTimeoutError):
                status = "timeout"
                trial.logger.warning("interim verifier timed out; the agent goes on")
            finally:
                await _hide_grader(trial, phase + 1)
                if not await _signal_agent_tree(trial, "CONT"):
                    raise RuntimeError("the agent process tree could not be resumed after verification")
            remaining = deadline - time.monotonic()
            passed = verifier_passed(interim)
            _record(trial, submission=phase + 1, remaining_sec=round(max(remaining, 0.0), 1), status=status,
                    reward=reward_of(interim), rewards=getattr(interim, "rewards", None), passed=passed,
                    feedback="binary", cut_short=cut_short)
            checkpoints.append({"phase": phase, "elapsed_sec": round(time.monotonic() - started, 1),
                                "reward": reward_of(interim), "passed": passed})
            if passed:
                break
            phase += 1
            instruction = rejection_text(phase=phase, remaining_sec=max(0, int(remaining)))
            trial.logger.info("continue_until_timeout: verification failed; resuming the agent "
                              "(continuation %s, %.0f s left)", phase, remaining)
    finally:
        if aggregated is not None:
            aggregated.metadata = {**(aggregated.metadata or {}), "continue_until_timeout_phases": phase,
                                   "continue_until_timeout_checkpoints": checkpoints,
                                   "verifier_feedback_mode": "binary", "continue_mode": "same_conversation"}
            trial.result.agent_result = aggregated


_INSTALLED = False


def install() -> bool:
    """Wrap SingleStepTrial._run_agent.  False when this Harbor has no such class (the LHTB fork has
    its own loop in trial.py)."""
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from harbor.agents.installed.base import NonZeroAgentExitCodeError
        from harbor.trial.errors import AgentTimeoutError
        from harbor.trial.single_step import SingleStepTrial
    except Exception:
        return False
    needed = ("_run_agent", "_run_agent_phase", "_run_shared_verifier", "_sync_agent_output", "_record_exception")
    missing = [m for m in needed if not hasattr(SingleStepTrial, m)]
    if missing:
        raise RuntimeError(f"optlimit: this Harbor's SingleStepTrial lacks {missing}; the continue-until-timeout "
                           "loop (optlimit/adapters/harbor25_continue.py) was written against Harbor 0.25")
    stock = SingleStepTrial._run_agent

    async def _run_agent(self) -> None:
        if not (enabled_by_env() and self._agent_timeout_sec is not None and task_wants_loop(self.task)):
            return await stock(self)
        self.logger.info("optlimit: continue-until-timeout loop (same conversation) for %s", self.task.name)
        try:
            await run_agent_until_timeout(self)
        except (AgentTimeoutError, NonZeroAgentExitCodeError) as exc:
            self._record_exception(exc)
        finally:
            await self._sync_agent_output(self.result)

    _run_agent._optlimit_stock = stock
    SingleStepTrial._run_agent = _run_agent
    _INSTALLED = True
    return True


def install_from_env() -> bool:
    return install() if enabled_by_env() else False
