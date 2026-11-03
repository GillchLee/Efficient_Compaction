"""The in-process compaction hook (needs the patched Harbor in the venv; skipped otherwise)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from optlimit.controller import config_from_spec

harbor = pytest.importorskip("harbor")
from optlimit.harbor_agent import OptlimitTerminus2  # noqa: E402


def test_config_from_spec_prior_prices_and_unknown_key():
    cfg = config_from_spec({"mode": "fixed", "fixed_limit": 80000, "lookahead": 0})
    assert cfg.mode == "fixed" and cfg.fixed_limit == 80000 and cfg.lookahead == 0
    cfg = config_from_spec({"mode": "formula", "prior": "codex", "prices": "claude-sonnet-5-5", "g_prior_weight": 3})
    assert cfg.Lp == 16000 and cfg.g_prior == 1900 and cfg.L0_mode == "fixed" and cfg.L0_fixed == 11800
    assert cfg.alpha == 25.0 and cfg.beta == 100.0 and cfg.g_prior_weight == 3
    with pytest.raises(ValueError):
        config_from_spec({"mode": "fixed", "fixed_limt": 1})


class FakeChat:
    def __init__(self):
        self.last_usage = None
        self.messages = []


def _agent(tmp_path, spec):
    a = OptlimitTerminus2(logs_dir=tmp_path, model_name="openai/gpt-4o-mini", optlimit=spec,
                          proactive_summarization_threshold=0, record_terminal_session=False)
    a._count_total_tokens = lambda chat: 0   # the stock safety-net check, kept offline
    return a


def test_fixed_limit_triggers_native_summarize(tmp_path):
    agent = _agent(tmp_path, {"mode": "fixed", "fixed_limit": 50000, "lookahead": 0})
    calls = []

    async def fake_summarize(chat, instruction, session):
        calls.append(chat.last_usage.prompt_tokens)
        return ("handoff prompt", None)

    agent._summarize = fake_summarize
    chat = FakeChat()

    async def step(prompt, cached):
        chat.last_usage = SimpleNamespace(prompt_tokens=prompt, cache_tokens=cached, completion_tokens=10)
        return await agent._check_proactive_summarization(chat, "task", None)

    assert asyncio.run(step(2000, 0)) is None          # first prompt: L0 observed, no compaction
    assert asyncio.run(step(30000, 2000)) is None
    # same usage object again (no new call happened): nothing is fed twice
    assert asyncio.run(agent._check_proactive_summarization(chat, "task", None)) is None
    assert asyncio.run(step(52000, 30000)) == ("handoff prompt", None)
    assert calls == [52000]
    # first prompt after the hand-off starts a new window; well below L -> no compaction
    assert asyncio.run(step(9000, 0)) is None

    recs = [json.loads(l) for l in (tmp_path / "optlimit_decisions.jsonl").read_text().splitlines()]
    assert [r["turn_idx"] for r in recs] == [1, 2, 3, 4]
    assert [r["compact_now"] for r in recs] == [False, False, True, False]
    assert recs[2]["action"] == "summarize" and recs[2]["limit"] == 50000
    assert recs[3]["window_start"] is True and recs[3]["window_idx"] == 1


def test_mode_off_never_compacts(tmp_path):
    agent = _agent(tmp_path, {"mode": "off"})
    agent._summarize = None  # would fail if called
    chat = FakeChat()
    for p in (1000, 100000, 500000):
        chat.last_usage = SimpleNamespace(prompt_tokens=p, cache_tokens=0, completion_tokens=1)
        assert asyncio.run(agent._check_proactive_summarization(chat, "task", None)) is None
    recs = [json.loads(l) for l in (tmp_path / "optlimit_decisions.jsonl").read_text().splitlines()]
    assert len(recs) == 3 and all(r["limit"] is None for r in recs)
