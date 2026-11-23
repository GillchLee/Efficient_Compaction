"""Continue-until-timeout loop for stock Harbor (optlimit/adapters/harbor25_continue.py), against a stub trial."""
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor.trial.single_step")      # stock Harbor >= 0.20 (the LHTB fork has its own loop)

from harbor.models.agent.context import AgentContext  # noqa: E402
from harbor.models.verifier.result import VerifierResult  # noqa: E402
from harbor.trial.errors import AgentTimeoutError  # noqa: E402

from optlimit.adapters import harbor25_continue as hc  # noqa: E402


class _Env:
    capabilities = SimpleNamespace(mounted=True)

    def __init__(self):
        self.calls = []

    async def exec(self, command, timeout_sec=None, **kw):
        self.calls.append("exec")
        return SimpleNamespace(return_code=0)

    async def empty_dirs(self, dirs, **kw):
        self.calls.append(("empty", [str(d) for d in dirs]))

    async def reset_dirs(self, *, remove_dirs, create_dirs, **kw):
        self.calls.append(("reset", [str(d) for d in remove_dirs], [str(d) for d in create_dirs]))


class _Trial:
    """What run_agent_until_timeout touches of a Harbor 0.25 SingleStepTrial."""

    def __init__(self, tmp_path: Path, rewards, budget=3600.0, phase_error=None):
        (tmp_path / "verifier").mkdir()
        self.paths = SimpleNamespace(trial_dir=tmp_path, verifier_dir=tmp_path / "verifier", artifacts_dir=tmp_path / "a")
        cfg = SimpleNamespace(agent=SimpleNamespace(user=None, continue_until_timeout=True),
                              verifier=SimpleNamespace(user=None, environment_mode=None),
                              environment=SimpleNamespace(os="linux"), steps=None)
        self.task = SimpleNamespace(config=cfg, instruction="TASK TEXT", name="t")
        self.result = SimpleNamespace(agent_result=None, agent_execution=None)
        self.agent_environment = _Env()
        self.logger = logging.getLogger("test")
        self._agent_timeout_sec = budget
        self._verifier_timeout_sec = 60
        self._rewards = list(rewards)
        self._phase_error = phase_error
        self.phases = []

    async def _run_agent_phase(self, *, target, instruction, timeout_sec, user, resume=False):
        self.phases.append((instruction, resume, timeout_sec))
        target.agent_result = AgentContext(n_input_tokens=100, n_cache_tokens=60, n_output_tokens=10, cost_usd=0.5,
                                           metadata={"n_episodes": 2})
        target.agent_execution = SimpleNamespace(started_at=len(self.phases), finished_at=len(self.phases) + 0.5)
        if self._phase_error and len(self.phases) == self._phase_error[0]:
            raise self._phase_error[1]

    async def _run_shared_verifier(self, *, timeout_sec, user):
        (self.paths.verifier_dir / "reward.txt").write_text(str(self._rewards[0]))     # the grader's output
        return VerifierResult(rewards={"reward": self._rewards.pop(0)})


@pytest.fixture(autouse=True)
def _shared_mode(monkeypatch):
    import harbor.models.task.verifier_mode as vm
    monkeypatch.setattr(vm, "resolve_task_verifier_mode", lambda cfg: vm.VerifierEnvironmentMode.SHARED)


@pytest.mark.asyncio
async def test_loop_resumes_the_same_conversation_until_the_verifier_passes(tmp_path):
    t = _Trial(tmp_path, rewards=[0.3, 0.5, 1.0])
    await hc.run_agent_until_timeout(t)
    assert [p[1] for p in t.phases] == [False, True, True]                    # run, then native resume twice
    assert t.phases[0][0] == "TASK TEXT"
    assert t.phases[1][0] == hc.rejection_text(phase=1, remaining_sec=int(t.phases[1][0].split("approximately ")[1].split()[0]))
    assert "TASK TEXT" not in t.phases[1][0] and "0.3" not in t.phases[1][0]  # binary: nothing from the verifier
    rows = [json.loads(x) for x in (tmp_path / "interim_verifier.jsonl").read_text().splitlines()]
    assert [(r["submission"], r["reward"], r["passed"]) for r in rows] == [(1, 0.3, False), (2, 0.5, False), (3, 1.0, True)]
    a = t.result.agent_result
    assert (a.n_input_tokens, a.n_cache_tokens, a.n_output_tokens, a.cost_usd) == (300, 180, 30, 1.5)
    assert a.metadata["n_episodes"] == 6 and a.metadata["continue_until_timeout_phases"] == 2
    assert t.result.agent_execution.started_at == 1                           # the first phase's start is kept


@pytest.mark.asyncio
async def test_grader_output_is_hidden_before_every_resume(tmp_path):
    t = _Trial(tmp_path, rewards=[0.0, 1.0])
    await hc.run_agent_until_timeout(t)
    calls = t.agent_environment.calls
    # per verification: freeze, (verify), empty /logs/verifier, remove /tests, thaw
    assert calls[:4] == ["exec", ("empty", ["/logs/verifier"]), ("reset", ["/tests"], []), "exec"]
    snap = tmp_path / "interim_verifier_artifacts" / "phase-0001" / "reward.txt"
    assert snap.read_text() == "0.0"                                          # host-side copy outside the mount


@pytest.mark.asyncio
async def test_budget_end_raises_the_agent_timeout_and_keeps_the_totals(tmp_path):
    t = _Trial(tmp_path, rewards=[0.0], budget=0.5, phase_error=(2, AgentTimeoutError("x")))
    orig = t._run_agent_phase
    t._run_agent_phase = lambda **kw: _sleep_then(orig, kw, t)
    with pytest.raises(AgentTimeoutError):
        await hc.run_agent_until_timeout(t)
    assert len(t.phases) == 2 and t.result.agent_result.n_input_tokens == 200
    assert t.result.agent_result.metadata["continue_until_timeout_phases"] == 1


async def _sleep_then(orig, kw, t):
    import asyncio
    if len(t.phases) == 1:
        await asyncio.sleep(0.6)          # the budget runs out inside the second phase
    await orig(**kw)


def test_install_wraps_single_step_trial_and_leaves_other_tasks_alone(monkeypatch):
    from harbor.trial.single_step import SingleStepTrial
    monkeypatch.setattr(hc, "_INSTALLED", False)
    stock = SingleStepTrial._run_agent
    try:
        assert hc.install() and hc.install()                                  # idempotent
        assert SingleStepTrial._run_agent is not stock and SingleStepTrial._run_agent._optlimit_stock is stock
        monkeypatch.delenv("HB_CONTINUE_MODE", raising=False)
        assert not hc.enabled_by_env() and not hc.install_from_env()
        monkeypatch.setenv("HB_CONTINUE_MODE", "same_conversation")
        assert hc.enabled_by_env()
    finally:
        SingleStepTrial._run_agent = stock


def test_task_flag_is_read_from_the_toml_when_harbor_drops_it(tmp_path):
    p = tmp_path / "task.toml"
    p.write_text('[agent]\ncontinue_until_timeout = true\ntimeout_sec = 5400\n')
    task = SimpleNamespace(config=SimpleNamespace(agent=SimpleNamespace()), paths=SimpleNamespace(config_path=p))
    assert hc.task_wants_loop(task)
    p.write_text('[agent]\ntimeout_sec = 5400\n')
    assert not hc.task_wants_loop(task)


def test_codex_resume_command_is_not_doubled():
    """Harbor 0.25 builds `codex exec resume --last` itself; the adapter must not add a second one."""
    import asyncio
    from optlimit.adapters.harbor_installed import OptlimitCodex
    from harbor.agents.installed.codex import Codex

    seen = []

    async def fake(self, environment, command, env=None, cwd=None, timeout_sec=None):
        seen.append(command)

    a = OptlimitCodex.__new__(OptlimitCodex)
    a._resume = True
    orig = Codex.exec_as_agent
    Codex.exec_as_agent = fake
    try:
        asyncio.run(OptlimitCodex.exec_as_agent(a, None, "codex exec resume --last --json -- 'go on'"))
        asyncio.run(OptlimitCodex.exec_as_agent(a, None, "codex exec --json -- 'go on'"))
    finally:
        Codex.exec_as_agent = orig
    assert seen[0].count("resume --last") == 1 and seen[1].count("resume --last") == 1
