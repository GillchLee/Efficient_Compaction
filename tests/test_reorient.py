"""Re-orientation instruction behind the compaction summary (optlimit/reorient.py, proxy path)."""
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from optlimit.controller import ControllerConfig, config_from_spec
from optlimit.dialects import OpenAIResponses
from optlimit.proxy import ProxyConfig, create_app
from optlimit.reorient import PARALLEL, REORIENT_MARK, SEQUENTIAL, reorient_block


def _exec(cid, cmd, out="ok"):
    return [{"type": "custom_tool_call", "call_id": cid, "name": "exec", "input": 'tools.exec_command({cmd:"%s"})' % cmd},
            {"type": "custom_tool_call_output", "call_id": cid, "output": out}]


def test_modes_and_config_field():
    assert reorient_block("") == "" and reorient_block(None) == "" and reorient_block("off") == ""
    assert reorient_block("parallel") == PARALLEL and reorient_block("Sequential") == SEQUENTIAL
    assert PARALLEL.startswith(REORIENT_MARK) and "ONE response" in PARALLEL and "ONE response" not in SEQUENTIAL
    with pytest.raises(ValueError):
        reorient_block("paralel")
    assert ControllerConfig().reorient == ""
    assert config_from_spec({"mode": "fixed", "fixed_limit": 40000, "reorient": "parallel"}).reorient == "parallel"


def test_inject_is_idempotent_for_a_reorient_only_block():
    d = OpenAIResponses()
    body = {"input": [{"type": "message", "role": "user", "content": "task"},
                      {"type": "compaction", "id": "cmp_1", "encrypted_content": "x"}] + _exec("c1", "ls")}
    assert d.inject_carry(body, PARALLEL)
    assert body["input"][2]["content"][0]["text"] == PARALLEL and not d.inject_carry(body, PARALLEL)


async def _run(tmp_path, **ctl):
    seen = []

    async def upstream(request):
        body = json.loads(await request.body())
        seen.append(body)
        n = sum(len(json.dumps(x)) for x in body["input"])
        return JSONResponse({"id": "r", "object": "response", "output": [],
                             "usage": {"input_tokens": 12000 + n, "input_tokens_details": {"cached_tokens": 12000},
                                       "output_tokens": 100, "total_tokens": 12100 + n}})

    cfg = ProxyConfig(trigger="off", log_path=str(tmp_path / "log.jsonl"),
                      controller=ControllerConfig(mode="fixed", fixed_limit=40000, **ctl),
                      transport=httpx.ASGITransport(app=Starlette(routes=[Route("/v1/responses", upstream, methods=["POST"])])))
    app = create_app(cfg)
    task = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "port the service"}]}
    h1, h2 = ({"x-client-request-id": "T", "x-codex-window-id": f"T:{i}"} for i in range(2))
    cmp1 = {"type": "compaction", "id": "cmp_1", "encrypted_content": "x"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        w0 = [task] + _exec("a", "cat src/a.rs", "A")
        await c.post("/v1/responses", json={"model": "m", "input": w0}, headers=h1)
        await c.post("/v1/responses", json={"model": "m", "input": [task, cmp1]}, headers=h2)           # window 1 starts
        w1 = [task, cmp1] + _exec("b", "cat src/a.rs", "A") + _exec("c", "cat src/b.rs", "B") + _exec("d", "git status", "")
        await c.post("/v1/responses", json={"model": "m", "input": w1}, headers=h2)                     # 3 calls in one response
    return seen, [json.loads(l) for l in open(tmp_path / "log.jsonl")]


@pytest.mark.asyncio
async def test_proxy_puts_the_instruction_behind_the_first_summary_and_keeps_it(tmp_path):
    seen, recs = await _run(tmp_path, reorient="parallel")
    assert all(PARALLEL not in json.dumps(x) for x in seen[0]["input"])       # nothing before a compaction
    for body in seen[1:]:
        assert body["input"][1]["type"] == "compaction" and body["input"][2]["content"][0]["text"] == PARALLEL
    assert seen[2]["input"][3]["type"] == "custom_tool_call"                  # the agent's items follow unchanged
    assert recs[1]["reorient"] == "parallel" and recs[1]["carry_injected"] is True
    assert recs[0]["new_tool_calls"] == 1 and recs[1]["new_tool_calls"] == 0 and recs[2]["new_tool_calls"] == 3


@pytest.mark.asyncio
async def test_proxy_leaves_requests_alone_when_off(tmp_path):
    seen, recs = await _run(tmp_path)
    assert all(REORIENT_MARK not in json.dumps(b) for b in seen) and "reorient" not in recs[1]
