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
    cfg = config_from_spec({"mode": "formula", "prior": "terminus2-gpt-6-luna", "prices": "gpt-6-luna", "g_prior_weight": 3})
    assert cfg.Lp == 3203 and cfg.g_prior == 717 and cfg.L0_mode == "fixed" and cfg.L0_fixed == 1258
    assert cfg.m == 2.178 and cfg.phi == 0.0267 and cfg.g_prior_weight == 3
    assert cfg.alpha == 10.0 and cfg.beta == 50.0
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


def test_handoff_budgets_change_only_the_three_prompts(tmp_path):
    stock = _agent(tmp_path, {"mode": "off"})
    sized = OptlimitTerminus2(logs_dir=tmp_path, model_name="openai/gpt-4o-mini", optlimit={"mode": "off"},
                              handoff={"summary_words": 2600, "min_questions": 12, "answer_words": 2000},
                              proactive_summarization_threshold=0, record_terminal_session=False)
    assert stock._summary_prompt("T") == harbor.agents.terminus_2.terminus_2.Terminus2._summary_prompt(stock, "T")
    assert "at least five" in stock._question_prompt("T", "S", "screen")
    s, q, a = sized._summary_prompt("T"), sized._question_prompt("T", "S", "screen"), sized._answer_prompt("Q1?")
    assert s.startswith(stock._summary_prompt("T")) and "approximately 2600 words" in s
    assert "at least 12, more if necessary" in q and "at least five" not in q and "**Original Task:** T" in q
    assert "approximately 2000 words" in a and a.endswith("Q1?")
    with pytest.raises(ValueError):
        OptlimitTerminus2(logs_dir=tmp_path, model_name="openai/gpt-4o-mini", optlimit={"mode": "off"},
                          handoff={"summary_tokens": 1}, proactive_summarization_threshold=0,
                          record_terminal_session=False)


def test_prices_reload_updates_controller(tmp_path, monkeypatch):
    # start-up reads the repo's configs/prices.json (gpt-6-luna: alpha 10, beta 50); the reload
    # path is pointed at a temp copy that we then change
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"models": {"gpt-6-luna": {"read": 0.01, "write": 0.1, "output": 0.5}}}))
    monkeypatch.setattr(OptlimitTerminus2, "_prices_path", staticmethod(lambda: prices))
    agent = OptlimitTerminus2(logs_dir=tmp_path, model_name="openai/gpt-4o-mini",
                              optlimit={"mode": "formula", "prices": "gpt-6-luna", "prices_reload": True, "Lp": 3203,
                                        "L0_mode": "fixed", "L0_fixed": 1258, "m": 2.178, "gamma": 1.508,
                                        "summary_out": 4242, "g_prior": 717, "g_prior_weight": 5},
                              proactive_summarization_threshold=0, record_terminal_session=False)
    agent._count_total_tokens = lambda chat: 0
    chat = FakeChat()

    async def step(prompt, cached):
        chat.last_usage = SimpleNamespace(prompt_tokens=prompt, cache_tokens=cached, completion_tokens=10)
        return await agent._check_proactive_summarization(chat, "task", None)

    asyncio.run(step(2000, 0))
    assert agent._ol_cfg.alpha == 10 and agent._ol_cfg.beta == 50
    # the price sheet changes on disk (write premium + dearer output): next decision adapts
    prices.write_text(json.dumps({"models": {"gpt-6-luna": {"read": 0.01, "write": 0.25, "output": 1.0}}}))
    import os, time
    os.utime(prices, (time.time() + 5, time.time() + 5))
    asyncio.run(step(2700, 2000))
    assert agent._ol_cfg.alpha == 25 and agent._ol_cfg.beta == 100
    recs = [json.loads(l) for l in (tmp_path / "optlimit_decisions.jsonl").open()]
    assert recs[-1]["prices_reloaded"] == {"alpha": 25.0, "beta": 100.0} and recs[-1]["alpha"] == 25.0
    # summary-call usage reaches the controller
    agent._update_subagent_metrics(SimpleNamespace(prompt_tokens=30000, completion_tokens=2000, cache_tokens=28000, cost_usd=0.0))
    s = agent._ctl.session("main")
    assert s._summ_prompt == 30000 and s._summ_out == 2000
