"""API server-side compaction support in the proxy (threshold rewrite, iterations usage)."""


def test_rewrite_api_compaction_threshold():
    from optlimit.proxy import rewrite_api_threshold
    a = {"context_management": {"edits": [{"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": 1}},
                                          {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 150000}}]}}
    assert rewrite_api_threshold(a, 87000)
    assert a["context_management"]["edits"][1]["trigger"]["value"] == 87000
    assert a["context_management"]["edits"][0]["trigger"]["value"] == 1       # context editing untouched
    o = {"context_management": [{"type": "compaction", "compact_threshold": 200000}]}
    assert rewrite_api_threshold(o, 46000) and o["context_management"][0]["compact_threshold"] == 46000
    assert not rewrite_api_threshold({"messages": []}, 1)


def test_anthropic_iterations_compaction_usage():
    from optlimit.dialects import Anthropic
    u = {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 50,
         "iterations": [{"type": "compaction", "input_tokens": 160000, "output_tokens": 3000},
                        {"type": "message", "input_tokens": 1000, "output_tokens": 50}]}
    n = Anthropic._norm(u)
    assert n["prompt"] == 1000 and n["api_compaction_input"] == 160000 and n["api_compaction_output"] == 3000
    assert "api_compaction_input" not in Anthropic._norm({"input_tokens": 5})


def test_claude_code_appended_summary_prompt_is_a_summary_call():
    """Claude Code 2.1.3xx appends its compaction prompt after the last tool result."""
    from optlimit.dialects import Anthropic
    body = {"messages": [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "fake step 431"},
                                     {"type": "text", "text": "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\n"
                                      "Your task is to create a detailed summary of the conversation so far, ..."}]}]}
    assert Anthropic().classify(body)[0] == "summary"
    plain = {"messages": [{"role": "user", "content": "please summarise the design doc"}]}
    assert Anthropic().classify(plain)[0] == "main"
