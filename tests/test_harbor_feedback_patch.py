"""patch 04: HB_VERIFIER_FEEDBACK=false resumes the agent with the neutral turn (needs the patched Harbor)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

harbor = pytest.importorskip("harbor")
from harbor.trial import trial as trial_mod  # noqa: E402


def _make_trial(tmp_path, monkeypatch, feedback: str):
    monkeypatch.setenv("HB_CONTINUE_MODE", "same_conversation")
    monkeypatch.setenv("HB_VERIFIER_FEEDBACK", feedback)
    monkeypatch.delenv("HB_PROCESS_REWARD", raising=False)
    T = trial_mod.Trial
    t = T.__new__(T)
    prompts = []

    class Agent:
        continue_until_timeout = True

        async def run(self, instruction, environment, context):
            context.metadata = {"termination_reason": "confirmed_task_complete"}

        async def resume_after_verifier_rejection(self, user_prompt, context):
            prompts.append(user_prompt)
            context.metadata = {"termination_reason": "timeout"}   # stop after one continuation

    t._agent = Agent()
    t._agent_timeout_sec = 1000
    t._task = SimpleNamespace(instruction="do the task", config=SimpleNamespace(
        verifier=SimpleNamespace(user="root"), agent=SimpleNamespace(continue_until_timeout=True)))
    t._environment = SimpleNamespace(default_user="user", capabilities=SimpleNamespace(mounted=True))
    t._trial_paths = SimpleNamespace(trial_dir=tmp_path, verifier_dir=tmp_path / "verifier")
    t._logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    t._result = SimpleNamespace(agent_result=None, agent_execution=SimpleNamespace(finished_at=None))
    t._process_reward_tracker = None
    t._process_reward_wall_started = None

    async def noop(*a, **k):
        return None
    t._signal_agent_process_tree = noop
    t._hide_shared_verifier_artifacts = noop
    t._invoke_hooks = noop

    async def interim():
        return SimpleNamespace(rewards={"reward": 0.5})
    t._run_interim_verifier = interim
    monkeypatch.setattr(trial_mod, "ProcessRewardConfig", SimpleNamespace(from_env=lambda: None))
    return t, prompts


@pytest.mark.parametrize("feedback,needle,other", [
    ("true", "did not pass verification", "has been recorded"),
    ("false", "has been recorded", "did not pass verification"),
])
def test_continuation_turn_follows_feedback_switch(tmp_path, monkeypatch, feedback, needle, other):
    t, prompts = _make_trial(tmp_path, monkeypatch, feedback)
    asyncio.run(t._execute_agent())
    assert len(prompts) == 1 and needle in prompts[0] and other not in prompts[0]
    rec = json.loads((tmp_path / "interim_verifier.jsonl").read_text().splitlines()[0])
    assert rec["reward"] == 0.5 and rec["feedback"] == ("binary" if feedback == "true" else "none")
