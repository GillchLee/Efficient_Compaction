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
