from optlimit.dialects import OpenAIResponses


def _msg(role, text):
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


ENV = _msg("user", "<environment_context><cwd>/app</cwd></environment_context>")


def test_responses_key_prefers_codex_thread_header():
    d = OpenAIResponses()
    body = {"instructions": "x", "input": [ENV, _msg("user", "task A")]}
    assert d.session_key(body, {"x-client-request-id": "t1"}) == "cx:t1"
    assert d.session_key(body, {"conversation_id": "t2"}) == "cx:t2"


def test_responses_key_without_headers_uses_all_leading_user_messages():
    d = OpenAIResponses()
    a = {"instructions": "x", "input": [ENV, _msg("user", "task A")]}
    b = {"instructions": "x", "input": [ENV, _msg("user", "task B")]}
    assert d.session_key(a) != d.session_key(b)
    # later tool items do not change the key
    a2 = {"instructions": "x", "input": a["input"] + [{"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c"}]}
    assert d.session_key(a2) == d.session_key(a)


def test_codex_summary_and_handoff_detection():
    d = OpenAIResponses()
    summ = {"input": [ENV, _msg("user", "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary")]}
    assert d.classify(summ) == ("summary", False)
    hand = _msg("user", "Another language model started to solve this problem and produced a summary")
    first = {"input": [ENV, hand]}
    assert d.classify(first) == ("main", True)
    later = {"input": [ENV, hand, {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c"},
                       {"type": "function_call_output", "call_id": "c", "output": "ok"}]}
    assert d.classify(later) == ("main", False)


def test_codex_compaction_trigger_item_is_a_summary_call():
    """Codex 0.16x: compaction request = same input + a trailing compaction_trigger item; the next
    window ends with the returned compaction item (seen live 2026-11-14, gpt-6-luna)."""
    d = OpenAIResponses()
    task = _msg("user", "fix the build")
    call = {"type": "custom_tool_call", "name": "exec"}
    out = {"type": "custom_tool_call_output", "output": "ok"}
    assert d.classify({"input": [ENV, task, call, out]}) == ("main", False)
    assert d.classify({"input": [ENV, task, call, out, {"type": "compaction_trigger"}]}) == ("summary", False)
    new_window = {"input": [ENV, task, {"type": "compaction", "id": "cmp_1", "encrypted_content": "x"}]}
    assert d.classify(new_window) == ("main", True)
    assert d.classify({"input": new_window["input"] + [call, out]}) == ("main", False)
