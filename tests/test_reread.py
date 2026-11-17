"""Re-reads after a compaction: the tracker, the cost term (A) and the carry-over block (B)."""
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from optlimit import cost_model as cm
from optlimit.controller import ControllerConfig, LimitController
from optlimit.dialects import Anthropic, OpenAIChat, OpenAIResponses
from optlimit.proxy import ProxyConfig, create_app
from optlimit.reread import RereadTracker, classify_call


def _exec(cid, cmd, out="ok"):
    return [{"type": "custom_tool_call", "name": "exec", "call_id": cid,
             "input": 'const r = await tools.exec_command({cmd:"%s"});' % cmd},
            {"type": "custom_tool_call_output", "call_id": cid, "output": out}]


def test_classify_call_reads_edits_and_paths():
    assert classify_call("exec", 'tools.exec_command({cmd:"sed -n \'1,80p\' src/a.rs && rg -n foo crates/b/lib.rs"})') \
        == (True, False, ["src/a.rs", "crates/b/lib.rs"])
    r, e, p = classify_call("exec", 'tools.apply_patch("*** Begin Patch\\n*** Update File: src/a.rs\\n@@")')
    assert (r, e) == (False, True) and "src/a.rs" in p
    assert classify_call("Read", '{"file_path": "/app/src/main.py"}') == (True, False, ["/app/src/main.py"])
    assert classify_call("Bash", '{"command": "cargo test -p common"}') == (False, False, [])
    assert classify_call("keystrokes", "cat notes.md\n")[0] is True


def test_tracker_counts_only_reads_of_files_from_an_earlier_window():
    t = RereadTracker()
    t.observe("1", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "v1")
    assert not t.close_turn(10)                 # window 0: nothing is a re-read
    t.observe("2", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "v1")
    assert not t.close_turn(10)                 # same window
    assert t.start_window() == (0, 0, 0.0)
    assert t.observe("3", "exec", 'tools.exec_command({cmd:"cat /app/src/a.rs"})', "v1")   # same file, absolute path
    assert t.close_turn(50)
    t.observe("4", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "v1")
    assert not t.close_turn(50)                 # second read in the same window is not counted again
    t.observe("5", "exec", 'tools.exec_command({cmd:"cargo build"})', "")
    assert not t.close_turn(50)
    assert t.observe("3", "exec", "dup id is ignored", "") is False
    assert t.start_window() == (1, 1, 50.0)


def test_carry_block_skips_files_edited_after_their_last_read():
    t = RereadTracker()
    t.observe("1", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "A-contents")
    t.observe("2", "exec", 'tools.exec_command({cmd:"cat src/b.rs"})', "B-contents")
    t.close_turn()
    t.start_window()
    t.observe("3", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "A-contents")
    t.observe("4", "exec", 'tools.exec_command({cmd:"cat src/b.rs"})', "B-contents")
    t.observe("5", "exec", 'tools.apply_patch("*** Begin Patch\\n*** Update File: src/b.rs\\n@@")', "Done")
    t.close_turn()
    t.start_window()
    block = t.carry_block(2000)
    assert "A-contents" in block and "B-contents" not in block     # b.rs changed after it was read
    assert t.carried == {"src/a.rs"}
    assert t.carry_block(0) == ""
    # a file that is not re-read any more fades out
    for _ in range(3):
        t.start_window()
    assert t.carry_block(2000) == ""


def test_tool_events_per_dialect():
    r = OpenAIResponses().tool_events({"input": _exec("c1", "cat a.py", "X") + [
        {"type": "function_call", "name": "shell", "arguments": '{"cmd": "ls"}', "call_id": "c2"}]})
    assert [(e["id"], e["output"]) for e in r] == [("c1", "X"), ("c2", None)]
    a = Anthropic().tool_events({"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "X"}]}]})
    assert a[0]["name"] == "Read" and a[0]["output"] == "X" and "a.py" in a[0]["text"]
    c = OpenAIChat().tool_events({"messages": [
        {"role": "assistant", "content": json.dumps({"analysis": "", "commands": [{"keystrokes": "cat a.py\n"}]})},
        {"role": "user", "content": "screen"}]})
    assert c[0]["text"] == "cat a.py\n" and c[0]["output"] == "screen" and c[0]["id"].startswith("ks:")


def test_inject_carry_goes_right_behind_the_compaction_item_and_is_idempotent():
    d = OpenAIResponses()
    body = {"input": [{"type": "message", "role": "user", "content": "task"},
                      {"type": "compaction", "id": "cmp_1", "encrypted_content": "x"}] + _exec("c1", "ls")}
    text = OpenAIResponses.CARRY_MARK + " ..."
    assert d.inject_carry(body, text)
    assert body["input"][2]["role"] == "user" and body["input"][2]["content"][0]["text"] == text
    assert body["input"][3]["type"] == "custom_tool_call"
    assert not d.inject_carry(body, text)                      # already there
    assert not d.inject_carry({"input": _exec("c1", "ls")}, text)   # no compaction item: first window


def test_reread_term_raises_the_limit_and_follows_observations():
    base = dict(mode="formula", Lp=1700, L0_mode="fixed", L0_fixed=12000, alpha=10, beta=50, m=1, gamma=1.4,
                summary_out=1700, g_prior=2000)
    off = LimitController(ControllerConfig(**base, reread_cost=False))
    on = LimitController(ControllerConfig(**base))              # default: on, prior 3.5 calls per compaction
    assert on.cfg.reread_charge and on.cfg.reread_turns == 3.5
    d_off, d_on = off.on_prompt("s", "main", 14000.0), on.on_prompt("s", "main", 14000.0)
    assert d_off.reread is None and d_on.reread > 0
    assert d_on.limit > d_off.limit * 1.3
    p = on.params(on.session("s"))
    assert abs(p.summary_fixed - d_on.reread) < 1e-6
    # observed windows pull the term towards what was seen; the prior keeps the weight of one window
    for _ in range(3):
        on.note_reread("s", 0, 0.0)
    assert d_off.limit < on.limit(on.session("s"))[0] < d_on.limit
    assert abs(on.reread_value(on.session("s")) - d_on.reread / 4) < 1
    on.note_reread("s", 6, 600_000.0)
    assert on.limit(on.session("s"))[0] > d_off.limit * 1.3
    assert on.call_cost(1000, 800, 10) == 800 + 10 * 200 + 50 * 10
    # fixed mode is untouched
    fx = LimitController(ControllerConfig(mode="fixed", fixed_limit=40000))
    assert fx.on_prompt("s", "main", 14000.0).limit == 40000


@pytest.mark.asyncio
async def test_proxy_reports_rereads_and_injects_the_carry_block(tmp_path):
    seen = []

    async def upstream(request):
        body = json.loads(await request.body())
        seen.append(body)
        n = sum(len(json.dumps(x)) for x in body["input"])
        return JSONResponse({"id": "r", "object": "response", "output": [],
                             "usage": {"input_tokens": 12000 + n, "input_tokens_details": {"cached_tokens": 12000},
                                       "output_tokens": 100, "total_tokens": 12100 + n}})

    cfg = ProxyConfig(trigger="off", log_path=str(tmp_path / "log.jsonl"),
                      controller=ControllerConfig(mode="formula", Lp=1700, L0_mode="fixed", L0_fixed=12000, alpha=10,
                                                  beta=50, m=1, summary_out=1700, g_prior=2000, carry_files=4000),
                      transport=httpx.ASGITransport(app=Starlette(routes=[Route("/v1/responses", upstream, methods=["POST"])])))
    app = create_app(cfg)
    task = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "port the service"}]}
    h1, h2, h3 = ({"x-client-request-id": "T", "x-codex-window-id": f"T:{i}"} for i in range(3))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        w0 = [task] + _exec("a", "cat src/a.rs", "A-contents")
        await c.post("/v1/responses", json={"model": "m", "input": w0}, headers=h1)
        await c.post("/v1/responses", json={"model": "m", "input": w0 + [{"type": "compaction_trigger"}]}, headers=h1)
        cmp1 = {"type": "compaction", "id": "cmp_1", "encrypted_content": "x"}
        await c.post("/v1/responses", json={"model": "m", "input": [task, cmp1]}, headers=h2)      # window 1 starts
        w1 = [task, cmp1] + _exec("b", "cat src/a.rs", "A-contents")                                 # the re-read
        await c.post("/v1/responses", json={"model": "m", "input": w1}, headers=h2)
        await c.post("/v1/responses", json={"model": "m", "input": w1 + [{"type": "compaction_trigger"}]}, headers=h2)
        cmp2 = {"type": "compaction", "id": "cmp_2", "encrypted_content": "y"}
        await c.post("/v1/responses", json={"model": "m", "input": [task, cmp2]}, headers=h3)      # window 2 starts
        await c.post("/v1/responses", json={"model": "m", "input": [task, cmp2] + _exec("c", "cargo build")}, headers=h3)
    s = app.state.controller.session("cx:T")
    assert s.rr_turn_obs == [1.0] and s.rr_cost_obs[0] > 0          # window 1 had one re-read call
    # nothing to carry into window 1 (no re-read known yet, nothing edited); a.rs is carried into window 2
    assert all(x.get("type") != "message" or "A-contents" not in json.dumps(x) for x in seen[2]["input"])
    for body in seen[5:]:
        assert body["input"][1]["type"] == "compaction"
        assert body["input"][2]["role"] == "user" and "A-contents" in body["input"][2]["content"][0]["text"]
    assert seen[5]["input"][2] == seen[6]["input"][2]              # identical block on every request of the window
    recs = [json.loads(l) for l in open(tmp_path / "log.jsonl")]
    assert recs[2].get("reread_turn") is None and recs[3].get("reread_turn") is True
    assert recs[5]["reread_window"] == {"window": 1, "turns": 1, "cost": round(s.rr_cost_obs[0])}
    assert recs[5]["carry"]["files"] == ["src/a.rs"]


def test_keystrokes_parser_and_terminus2_feed():
    from optlimit.dialects import _keystrokes
    assert _keystrokes('{"analysis": "x", "commands": [{"keystrokes": "cat a.py\\n", "duration": 1}], "task_complete": false}') \
        == ["cat a.py\n"]
    assert _keystrokes("no json here") == []
    t = RereadTracker()
    t.observe(None, "keystrokes", "cat a.py\n")
    t.close_turn(1)
    t.start_window()
    assert t.observe(None, "keystrokes", "sed -n '1,40p' a.py\n") and t.close_turn(7)
    assert t.start_window() == (1, 1, 7.0)


def test_inject_carry_behind_a_plain_text_handoff_message():
    """Codex local compaction: the new window carries the summary as a user message, no `compaction` item."""
    d = OpenAIResponses()
    hand = {"type": "message", "role": "user", "content": [{"type": "input_text",
            "text": "Another language model started to solve this problem and produced a summary of its work."}]}
    body = {"input": [{"type": "message", "role": "user", "content": "task"}, hand] + _exec("c1", "ls")}
    assert d.inject_carry(body, OpenAIResponses.CARRY_MARK + " x")
    assert body["input"][2]["content"][0]["text"].startswith(OpenAIResponses.CARRY_MARK)
    assert body["input"][3]["type"] == "custom_tool_call" and not d.inject_carry(body, OpenAIResponses.CARRY_MARK + " x")


def test_reread_charge_alias_and_names():
    from optlimit.controller import ControllerConfig
    assert ControllerConfig().reread_charge is True
    assert ControllerConfig(reread_cost=False).reread_charge is False      # old spelling still works
    assert ControllerConfig(reread_charge=False).reread_charge is False
    assert ControllerConfig().rent_net_reread is False and ControllerConfig().carry_files == 0


def test_net_rent_leaves_reread_growth_out():
    from optlimit.controller import ControllerConfig, LimitController
    base = dict(mode="balance", Lp=8000, alpha=10, beta=0, g_prior=1000, g_prior_weight=50,
                reread_charge=False, learn_recent=0, min_window_turns=2)
    # window: a 6K jump from a re-read right after the start, then steady 1K growth
    seq = [10000, 16000] + [16000 + 1000 * i for i in range(1, 60)]

    def run(net: bool) -> int:
        ctl = LimitController(ControllerConfig(**base, rent_net_reread=net))
        for i, p in enumerate(seq):
            if i == 1:
                ctl.note_reread_growth("s")          # prompt 1 carries the output of a re-read call
            d = ctl.on_prompt("s", "main", float(p))
            if d.compact_now:
                return p
        return -1

    gross, net = run(False), run(True)
    assert 0 < gross < net
    # the 6K jump no longer counts: net rent fires about 6K later, where a window starting at 16K would
    assert net - gross >= 3000


def test_net_rent_resets_each_window():
    from optlimit.controller import ControllerConfig, LimitController
    ctl = LimitController(ControllerConfig(mode="balance", Lp=1000, alpha=10, g_prior=1000, reread_charge=False,
                                           rent_net_reread=True))
    ctl.on_prompt("s", "main", 10000.0)
    ctl.note_reread_growth("s")
    d = ctl.on_prompt("s", "main", 15000.0)
    assert d.rr_growth == 5000
    d = ctl.on_prompt("s", "main", 4000.0, window_start_hint=True)
    assert d.window_start and d.rr_growth == 0


def test_carry_module_matches_tracker_wrapper():
    from optlimit import carry
    from optlimit.reread import RereadTracker
    tr = RereadTracker()
    tr.observe("1", "exec_command", '{"cmd":"cat src/a.rs"}', "fn a() {}\n" * 100)
    tr.start_window()
    tr.observe("2", "exec_command", '{"cmd":"cat src/a.rs"}', "fn a() {}\n" * 100)
    tr.start_window()
    assert tr.carry_block(4000) == carry.snapshot_block(tr, 4000)
    assert "src/a.rs" in carry.ranked_files(tr)
