#!/usr/bin/env python3
"""Fake Anthropic Messages / OpenAI Responses upstream for checking the optlimit proxy against the
real Claude Code / Codex CLIs without API spend (or keys).

    python scripts/fake_llm_upstream.py --port 8777 --base-prompt 30000 &
    OPTLIMIT_ANTHROPIC_UPSTREAM=http://127.0.0.1:8777 OPTLIMIT_OPENAI_UPSTREAM=http://127.0.0.1:8777 \
        bash scripts/run_harness_job.sh configs/sweep/<smoke>.yaml

Every request gets a short text answer (optionally after --tool-rounds Bash tool calls) and a usage block whose prompt size is
--base-prompt + len(request body) / 4, mostly cache reads. A compaction request (the harness's
own summary prompt) gets a short summary. Each request is appended to --log, so the harness's
behaviour (did it compact after the proxy's signal? what did the next window start with?) can be
read without the proxy. The answers never solve a task: the verifier rejects, Harbor resumes the
same conversation, and the harness's pre-turn compaction check runs on the reported usage.
"""
from __future__ import annotations

import argparse
import json
import time

SUMMARY_MARKERS = ("create a detailed summary", "CONTEXT CHECKPOINT COMPACTION", "summary of the conversation")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_text_of(c.get("text") or c.get("content") or "") if isinstance(c, dict) else str(c)
                        for c in content)
    return ""


def _tool_rounds(msgs: list) -> int:
    """Trailing (assistant tool_use, user tool_result) pairs: tool calls made in the current turn."""
    n, i = 0, len(msgs) - 1
    while i >= 1:
        u, a = msgs[i], msgs[i - 1]
        uc, ac = u.get("content"), a.get("content")
        if (u.get("role") == "user" and isinstance(uc, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in uc)
                and a.get("role") == "assistant" and isinstance(ac, list)
                and any(isinstance(c, dict) and c.get("type") == "tool_use" for c in ac)):
            n += 1
            i -= 2
        else:
            break
    return n


def make_app(base_prompt: int, log_path: str | None, tool_rounds: int = 0):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    seq = {"n": 0}

    def record(kind: str, body: dict, last_user: str, prompt: int) -> None:
        seq["n"] += 1
        if log_path:
            with open(log_path, "a") as f:
                f.write(json.dumps({"n": seq["n"], "ts": time.time(), "api": kind, "model": body.get("model"),
                                    "prompt": prompt, "summary_request": _is_summary(last_user),
                                    "last_user": last_user[:300]}) + "\n")

    def _is_summary(text: str) -> bool:
        return any(m.lower() in text.lower() for m in SUMMARY_MARKERS)

    def sse(events: list[tuple[str, dict]]):
        async def gen():
            for name, data in events:
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
        return StreamingResponse(gen(), media_type="text/event-stream")

    async def messages(request: Request):
        raw = await request.body()
        body = json.loads(raw)
        msgs = body.get("messages") or []
        last_user = next((_text_of(m.get("content")) for m in reversed(msgs) if m.get("role") == "user"), "")
        prompt = base_prompt + len(raw) // 4
        summary = _is_summary(last_user)
        record("anthropic", body, last_user, prompt)
        text = ("<summary>Fake summary: the task was read, nothing was solved yet.</summary>" if summary
                else f"Fake answer {seq['n']}: I looked at the task.")
        tool_names = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
        use_tool = (not summary and "Bash" in tool_names and _tool_rounds(msgs) < tool_rounds)
        usage = {"input_tokens": 50, "cache_creation_input_tokens": 500,
                 "cache_read_input_tokens": prompt - 550, "output_tokens": 20}
        if use_tool:
            block = {"type": "tool_use", "id": f"toolu_fake_{seq['n']}", "name": "Bash",
                     "input": {"command": f"echo fake step {seq['n']}", "description": "fake step"}}
            stop = "tool_use"
        else:
            block, stop = {"type": "text", "text": text}, "end_turn"
        msg = {"id": f"msg_fake_{seq['n']}", "type": "message", "role": "assistant", "model": body.get("model"),
               "content": [block], "stop_reason": stop, "stop_sequence": None, "usage": usage}
        if not body.get("stream"):
            return JSONResponse(msg)
        start = dict(msg, content=[], stop_reason=None, usage=dict(usage, output_tokens=1))
        if use_tool:
            blocks = [("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": dict(block, input={})}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "input_json_delta",
                                                         "partial_json": json.dumps(block["input"])}})]
        else:
            blocks = [("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": text}})]
        return sse([
            ("message_start", {"type": "message_start", "message": start}),
            *blocks,
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": {"output_tokens": 20}}),
            ("message_stop", {"type": "message_stop"}),
        ])

    async def count_tokens(request: Request):
        raw = await request.body()
        return JSONResponse({"input_tokens": base_prompt + len(raw) // 4})

    async def responses(request: Request):
        raw = await request.body()
        body = json.loads(raw)
        items = body.get("input") if isinstance(body.get("input"), list) else []
        last_user = next((_text_of(i.get("content")) for i in reversed(items)
                          if isinstance(i, dict) and i.get("role") == "user"), "")
        prompt = base_prompt + len(raw) // 4
        summary = _is_summary(last_user)
        record("responses", body, last_user, prompt)
        text = ("Fake checkpoint summary: task read, nothing solved yet." if summary
                else f"Fake answer {seq['n']}: I looked at the task.")
        rid = f"resp_fake_{seq['n']}"
        item = {"id": f"msg_fake_{seq['n']}", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
        usage = {"input_tokens": prompt, "input_tokens_details": {"cached_tokens": prompt - 550},
                 "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": prompt + 20}
        resp = {"id": rid, "object": "response", "status": "completed", "model": body.get("model"),
                "output": [item], "usage": usage}
        return sse([
            ("response.created", {"type": "response.created", "response": dict(resp, status="in_progress", output=[])}),
            ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                            "item": dict(item, status="in_progress", content=[])}),
            ("response.output_text.delta", {"type": "response.output_text.delta", "output_index": 0,
                                            "content_index": 0, "item_id": item["id"], "delta": text}),
            ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
            ("response.completed", {"type": "response.completed", "response": resp}),
        ])

    async def models(_: Request):
        return JSONResponse({"data": [], "object": "list"})

    return Starlette(routes=[
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/messages/count_tokens", count_tokens, methods=["POST"]),
        Route("/v1/responses", responses, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--base-prompt", type=int, default=30000, help="prompt tokens reported on top of len(body)/4")
    ap.add_argument("--log")
    ap.add_argument("--tool-rounds", type=int, default=0,
                    help="Anthropic: answer with this many Bash tool calls per turn before the final text, so the "
                         "harness makes further calls inside one run (where its compaction check runs)")
    a = ap.parse_args()
    import uvicorn
    uvicorn.run(make_app(a.base_prompt, a.log, a.tool_rounds), host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
