"""The harness contract on a fake model: prompt grows by g per turn, compaction at a fixed limit."""
from optlimit.adapters.chat_loop import CompactingChatLoop
from optlimit.controller import config_from_spec
from optlimit.usage import normalize_usage


def test_normalize_usage_variants():
    assert normalize_usage({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 800},
                            "completion_tokens": 50}) == normalize_usage(
        {"input_tokens": 200, "cache_read_input_tokens": 800, "output_tokens": 50})
    u = normalize_usage({"input_tokens": 100, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 400})
    assert (u.prompt, u.cached, u.written) == (1000, 500, 400)
    assert normalize_usage(None).prompt == 0


class FakeModel:
    """Prompt = 20000 + 2000 per user turn, all of it cached except the newest 2000 (g = 2000)."""
    def __init__(self):
        self.base, self.g = 20000, 2000

    def call(self, messages):
        n = sum(1 for m in messages if m["role"] == "user")
        prompt = self.base + self.g * n
        return "ok", {"prompt_tokens": prompt, "prompt_tokens_details": {"cached_tokens": prompt - self.g},
                      "completion_tokens": 300}

    def summarize(self, messages):
        # hand-off of ~10K: the next window starts from [first message, summary] at base + 10000
        self.base = 30000
        usage = {"prompt_tokens": 20000 + self.g * len(messages) // 2, "completion_tokens": 4000}
        return [messages[0], {"role": "assistant", "content": "summary"}], [usage]


def test_fixed_limit_compacts_and_restarts_window():
    m = FakeModel()
    loop = CompactingChatLoop(config_from_spec({"mode": "fixed", "fixed_limit": 60000, "lookahead": 0}),
                              call=m.call, summarize=m.summarize)
    for i in range(30):
        loop.step(f"turn {i}")
    acts = [d["action"] for d in loop.decisions]
    assert acts[19] == "summarize" and loop.n_compactions >= 1         # 20000 + 2000*20 = 60000 reaches the limit
    first = acts.index("summarize")
    assert loop.decisions[first + 1]["window_start"] is True           # the next prompt opened a new window
    assert loop.decisions[first + 1]["window_idx"] == 1
    assert loop.decisions[first + 1]["prompt"] < 60000                 # history was replaced by the hand-off


def test_formula_mode_learns_g_and_sets_limit():
    m = FakeModel()
    cfg = config_from_spec({"mode": "formula", "prior": "terminus2-gpt-6-luna-1.0", "prices": "gpt-6-luna",
                            "g_prior_weight": 0, "lookahead": 1})
    loop = CompactingChatLoop(cfg, call=m.call, summarize=m.summarize)
    for i in range(6):
        loop.step(f"turn {i}")
    d = loop.decisions[-1]
    assert abs(d["g_hat"] - 2000) < 1e-6 and d["limit"] is not None and d["limit"] > 20000
