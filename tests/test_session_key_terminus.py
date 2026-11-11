"""Regression: concurrent terminus-2 trials must not share one proxy session.

Harbor's terminus-2 embeds the task instruction ~3 KB into a fixed prompt template; a
prefix-based root key collapsed every trial of a job into one controller session
(SWE-Marathon balance run, 2026-11-11)."""
from optlimit.dialects import Anthropic, OpenAIChat

TEMPLATE = "You are an AI assistant tasked with solving command-line tasks. " * 60  # ~3.8 KB


def _t2_body(instruction: str, extra=()):
    first = TEMPLATE + "\nTask: " + instruction + "\n\nCurrent terminal state:\n$ "
    msgs = [{"role": "user", "content": first}] + list(extra)
    return {"model": "claude-haiku-5-5", "messages": msgs}


def test_anthropic_root_key_separates_tasks_sharing_a_template():
    d = Anthropic()
    a = _t2_body("rewrite the zstd decoder in Rust")
    b = _t2_body("implement a VLIW kernel scheduler")
    assert d.session_key(a) != d.session_key(b)


def test_anthropic_root_key_survives_terminus2_summarization():
    d = Anthropic()
    a = _t2_body("rewrite the zstd decoder in Rust")
    # terminus-2 keeps messages[0] and replaces the rest with the question prompt + questions
    after = _t2_body("rewrite the zstd decoder in Rust", extra=[
        {"role": "user", "content": "Original Task: ... Summary: ... what questions do you have?"},
        {"role": "assistant", "content": "1. Which files did you change?"},
    ])
    assert d.session_key(after) == d.session_key(a)


def test_anthropic_key_prefers_claude_code_session_id():
    d = Anthropic()
    body = _t2_body("x")
    body["metadata"] = {"user_id": '{"session_id": "abc"}'}
    assert d.session_key(body) == "cc:abc"


def test_openai_chat_root_key_separates_tasks_sharing_a_template():
    d = OpenAIChat()
    a = _t2_body("task A")
    b = _t2_body("task B")
    assert d.session_key(a) != d.session_key(b)


def test_anthropic_root_key_ignores_merged_question_block():
    """litellm merges consecutive user messages into one message with several text blocks."""
    d = Anthropic()
    a = _t2_body("rewrite the zstd decoder in Rust")
    first = a["messages"][0]["content"]
    merged = {"model": "claude-haiku-5-5", "messages": [
        {"role": "user", "content": [{"type": "text", "text": first},
                                     {"type": "text", "text": "Original Task: ... Summary: ... questions?"}]},
        {"role": "assistant", "content": "1. Which files did you change?"},
        {"role": "user", "content": "Answers: ... continue."},
    ]}
    assert d.session_key(merged) == d.session_key(a)
    # a different task with the same merged shape still differs
    b = _t2_body("implement a VLIW kernel scheduler")
    assert d.session_key(merged) != d.session_key(b)
