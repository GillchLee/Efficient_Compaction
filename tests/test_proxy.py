"""Proxy tests against a fake upstream that speaks all three dialects (streaming and not)."""
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from optlimit.controller import ControllerConfig
from optlimit.proxy import ProxyConfig, create_app


def _count(body: dict) -> int:
    """Fake tokenizer: 1 token per 4 chars of the whole message array."""
    key = "input" if "input" in body else "messages"
    return len(json.dumps(body.get(key, ""))) // 4 + 100


async def anthropic(request: Request):
    body = await request.json()
    p = _count(body)
    if body.get("stream"):
        async def gen():
            yield f'event: message_start\ndata: {json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": p - 30, "output_tokens": 1}}})}\n\n'.encode()
            yield b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
            yield b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
            yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({"type": "message", "content": [{"type": "text", "text": "hi"}],
                         "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                                   "cache_read_input_tokens": p - 30, "output_tokens": 7}})


async def chat(request: Request):
    body = await request.json()
    p = _count(body)
    assert not body.get("stream") or body["stream_options"]["include_usage"]
    if body.get("stream"):
        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield ('data: ' + json.dumps({"choices": [], "usage": {"prompt_tokens": p, "completion_tokens": 7,
                                                                    "total_tokens": p + 7,
                                                                    "prompt_tokens_details": {"cached_tokens": p - 50}}}) + '\n\n').encode()
            yield b'data: [DONE]\n\n'
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                         "usage": {"prompt_tokens": p, "completion_tokens": 7, "total_tokens": p + 7,
                                   "prompt_tokens_details": {"cached_tokens": p - 50}}})


async def responses(request: Request):
    body = await request.json()
    p = _count(body)

    async def gen():
        yield b'event: response.created\ndata: {"type":"response.created"}\n\n'
        yield ('event: response.completed\ndata: ' + json.dumps({"type": "response.completed", "response": {
            "usage": {"input_tokens": p, "output_tokens": 7, "total_tokens": p + 7,
                      "input_tokens_details": {"cached_tokens": p - 50}}}}) + '\n\n').encode()
    return StreamingResponse(gen(), media_type="text/event-stream")


upstream = Starlette(routes=[Route("/v1/messages", anthropic, methods=["POST"]),
                             Route("/v1/chat/completions", chat, methods=["POST"]),
                             Route("/v1/responses", responses, methods=["POST"])])


def make(tmp_path, trigger, **ctl):
    cfg = ProxyConfig(anthropic_upstream="http://up", openai_upstream="http://up", trigger=trigger,
                      log_path=str(tmp_path / "log.jsonl"), signal_high=900_000, signal_low_cap=50_000,
                      controller=ControllerConfig(**ctl), transport=httpx.ASGITransport(app=upstream))
    return create_app(cfg), cfg


def grow(messages, n):
    messages = list(messages)
    messages.append({"role": "assistant", "content": "x" * 400})
    messages.append({"role": "user", "content": "y" * 400 * n})
    return messages


async def test_anthropic_usage_rewrite_until_compaction(tmp_path):
    app, cfg = make(tmp_path, "usage", mode="fixed", fixed_limit=3000, g_prior=200, Lp=500, min_window_turns=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        msgs = [{"role": "user", "content": "task " * 50}]
        fired_at = None
        for t in range(40):
            body = {"model": "m", "messages": msgs, "stream": t % 2 == 0,
                    "metadata": {"user_id": json.dumps({"session_id": "S1"})}}
            r = await c.post("/v1/messages", json=body)
            assert r.status_code == 200
            text = r.text
            if "message_start" in text:
                ev = [json.loads(l[6:]) for l in text.splitlines() if l.startswith("data: ")]
                usage = ev[0]["message"]["usage"]
            else:
                usage = r.json()["usage"]
            if usage["input_tokens"] >= 900_000:
                fired_at = t
                break
            assert usage["input_tokens"] <= 50_000
            msgs = grow(msgs, 3)
        assert fired_at is not None
    recs = [json.loads(l) for l in open(cfg.log_path)]
    true = [r["prompt"] for r in recs]
    assert true[-1] + 200 >= 3000
    assert all(r["session"] == "cc:S1" for r in recs)
    assert recs[-1]["decision"]["compact_now"] and recs[-1]["reported_prompt"] == 900_000


async def test_openai_chat_error_trigger_then_window_restart(tmp_path):
    app, cfg = make(tmp_path, "error", mode="fixed", fixed_limit=2500, g_prior=100, Lp=300, min_window_turns=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task " * 50}]
        got_error = False
        for t in range(40):
            r = await c.post("/v1/chat/completions", json={"model": "m", "messages": msgs, "stream": True})
            if r.status_code == 400:
                assert "context_length_exceeded" in r.text
                got_error = True
                break
            msgs = grow(msgs, 2)
        assert got_error
        # harness summarises (summary call passes through) and restarts with a short history
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": msgs + [
            {"role": "user", "content": "You are about to hand off your work to another AI agent. Summarise."}]})
        assert r.status_code == 200
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": msgs[:2] + [
            {"role": "user", "content": "Here are the answers the other agent provided.\n\nContinue."}]})
        assert r.status_code == 200 and r.json()["usage"]["prompt_tokens"] < 2500  # error mode never rewrites
    recs = [json.loads(l) for l in open(cfg.log_path)]
    kinds = [(r["kind"], r["action"]) for r in recs]
    assert ("main", "rejected") in kinds
    assert ("summary", "forwarded") in kinds
    assert recs[-1]["decision"]["window_start"] and recs[-1]["decision"]["window_idx"] == 1
    ctl = app.state.controller
    s = next(iter(ctl.sessions.values()))
    assert s.n_compactions == 1 and not s.pending_compact


async def test_responses_stream_usage_rewrite_and_shadow(tmp_path):
    app, cfg = make(tmp_path, "off", mode="formula", Lp=300, alpha=10, g_prior=100)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task " * 50}]}]
        for _ in range(3):
            r = await c.post("/v1/responses", json={"model": "m", "instructions": "be good", "input": items, "stream": True})
            assert r.status_code == 200
            done = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")][-1]
            assert done["response"]["usage"]["input_tokens"] < 50_000  # untouched in shadow
            items = items + [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x" * 400}]},
                             {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "y" * 800}]}]
    recs = [json.loads(l) for l in open(cfg.log_path)]
    assert [r["decision"]["turn_idx"] for r in recs] == [1, 2, 3]
    assert recs[-1]["decision"]["limit"] is not None and recs[-1]["reported_prompt"] is None
    assert recs[1]["decision"]["g_hat"] > 100  # grew after observing a delta


async def test_usage_mode_rewrites_responses_dialect(tmp_path):
    app, cfg = make(tmp_path, "usage", mode="fixed", fixed_limit=100, Lp=10, g_prior=10, min_window_turns=1,
                    L0_mode="fixed", L0_fixed=0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task " * 200}]}]
        r = await c.post("/v1/responses", json={"model": "m", "input": items, "stream": True})
        done = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")][-1]
        assert done["response"]["usage"]["input_tokens"] == 900_000
        assert done["response"]["usage"]["total_tokens"] == 900_007
