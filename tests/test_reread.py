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


def test_carry_order_reread_edited_puts_files_reread_and_edited_first():
    """carry_order="reread_edited": a file the last window both re-read and edited outranks a file
    with a higher re-read score; the default order is unchanged."""
    from optlimit.carry import ranked_files

    t = RereadTracker()
    for i, f in enumerate(("a", "b")):
        t.observe(f"w0-{i}", "exec", 'tools.exec_command({cmd:"cat src/%s.rs"})' % f, f.upper() + "-v0")
    t.start_window()
    t.observe("w1-a", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "A-v0")        # a: re-read twice
    t.start_window()
    t.observe("w2-a", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "A-v0")
    t.observe("w2-b", "exec", 'tools.exec_command({cmd:"cat src/b.rs"})', "B-v0")        # b: re-read once ...
    t.observe("w2-e", "exec", 'tools.apply_patch("*** Begin Patch\\n*** Update File: src/b.rs\\n@@")', "Done")
    t.observe("w2-b2", "exec", 'tools.exec_command({cmd:"cat src/b.rs"})', "B-v1")       # ... edited, read again
    t.start_window()
    assert t.hot == {"src/b.rs"}
    assert t.score["src/a.rs"] > t.score["src/b.rs"]
    assert ranked_files(t) == ["src/a.rs", "src/b.rs"]
    assert ranked_files(t, "reread_edited") == ["src/b.rs", "src/a.rs"]
    # with room for one item only, the order decides which file is carried
    assert "A-v0" in t.carry_block(150, per_item_tokens=150)
    assert "B-v1" in t.carry_block(150, per_item_tokens=150, order="reread_edited")
    t.start_window()
    assert t.hot == set()                              # only the last finished window counts
    with pytest.raises(ValueError):
        ranked_files(t, "nope")


def test_productive_turn_charge_minimises_cost_per_productive_turn():
    """reread_productive: the limit is the minimiser of window cost / (K - r), found here by brute force."""
    from dataclasses import replace as _replace

    base = dict(mode="formula", Lp=1700, L0_mode="fixed", L0_fixed=11500, alpha=10, beta=50, m=1, gamma=1.4,
                summary_out=1700, g_prior=2000)
    plain = LimitController(ControllerConfig(**base))
    prod = LimitController(ControllerConfig(**base, reread_productive=True))
    assert not plain.cfg.reread_productive                       # off by default
    d0, d1 = plain.on_prompt("s", "main", 12000.0), prod.on_prompt("s", "main", 12000.0)
    assert d1.limit > d0.limit                                   # the exact optimum is above the default charge
    assert 1.05 < d1.limit / d0.limit < 1.12                     # 47.8K -> ~51.6K with the Luna medium prior

    def best(r):
        s = prod.session("s")
        p0 = _replace(prod.params(s), summary_fixed=0.0)         # the model without any re-read term
        def h(L):
            K = cm.turns_per_window(L, p0)
            return (cm.f(L, p0) + 50 * 300.0) * K / (K - r) if K > r + 0.5 else float("inf")
        return min(range(15000, 150000, 50), key=h)

    assert abs(d1.limit - best(3.5)) < 200
    # r follows the observed re-read calls (prior = one window), the observed cost is ignored
    for _ in range(3):
        prod.note_reread("s", 6, 1.0)
    r = (3.5 + 18) / 4
    assert abs(prod.limit(prod.session("s"))[0] - best(r)) < 200
    for _ in range(3):
        prod.note_reread("s", 0, 9e9)
    assert abs(prod.limit(prod.session("s"))[0] - best(3.5 / 4)) < 200
    # needs the Re-read Charge
    off = LimitController(ControllerConfig(**base, reread_charge=False, reread_productive=True))
    assert off.on_prompt("s", "main", 12000.0).reread is None


# ---------------------------------------------------------------- Whole-file Carry-over
def _fake_env(files):
    """run(cmd) for carry.whole_block: serves `files` (absolute path -> text) like the read_command shell snippet."""
    import re as _re

    async def run(cmd):
        n = int(_re.search(r"head -c (\d+)", cmd).group(1))
        for path, text in files.items():
            base, name = path.rsplit("/", 1)
            if f"-f {path} ]" in cmd or (_re.search(rf"\b{_re.escape(name)}\b", cmd) and
                                         any(path == f"{d}/{rel}" for d in _re.findall(r"(/[\w/.-]+)", cmd.split(";")[0])
                                             for rel in [path[len(d) + 1:]] if path.startswith(d + "/") and f"/{rel} ]" in cmd)):
                return 0, text[:n]
        return 1, ""
    return run


def test_streak_counts_consecutive_reread_windows_and_survives_while_carried_whole():
    t = RereadTracker()
    t.observe("a", "exec", 'tools.exec_command({cmd:"cat src/a.rs",workdir:"/app"})', "A")
    t.observe("b", "exec", 'tools.exec_command({cmd:"cat src/b.rs",workdir:"/app"})', "B")
    t.start_window()
    assert t.streak == {} and t.workdirs == {"/app": 2}
    t.observe("c", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "A")
    t.observe("d", "exec", 'tools.exec_command({cmd:"cat src/b.rs"})', "B")
    t.start_window()
    assert t.streak == {"src/a.rs": 1, "src/b.rs": 1}
    t.observe("e", "exec", 'tools.exec_command({cmd:"cat src/a.rs"})', "A")     # b.rs not re-read: its streak ends
    t.start_window()
    assert t.streak == {"src/a.rs": 2}
    t.whole = {"src/a.rs"}                                                      # carried whole in this window ...
    t.observe("f", "apply_patch", "*** Begin Patch\n*** Update File: src/a.rs\n@@\n-x\n+y\n*** End Patch")
    t.start_window()
    assert t.streak == {"src/a.rs": 2} and t.whole == set()                     # ... and edited, not re-read: stays
    t.whole = {"src/a.rs"}
    t.observe("g", "exec", 'tools.exec_command({cmd:"cargo build"})', "ok")
    t.start_window()
    assert t.streak == {}                                                       # carried but untouched: back to normal


@pytest.mark.asyncio
async def test_whole_block_carries_complete_files_only_and_keeps_them_out_of_the_snapshot_block():
    from optlimit import carry
    t = RereadTracker()
    for i in range(3):                                   # a.rs, big.json re-read in windows 1 and 2; c.rs only in 2
        t.observe(f"a{i}", "exec", 'tools.exec_command({cmd:"sed -n 1,5p src/a.rs",workdir:"/app"})', "A-old-part")
        t.observe(f"g{i}", "exec", 'tools.exec_command({cmd:"cat /tmp/big.json"})', "{...}")
        if i:
            t.observe(f"c{i}", "exec", 'tools.exec_command({cmd:"cat src/c.rs"})', "C-snapshot")
        t.start_window()
    assert carry.whole_candidates(t, 2) == ["/tmp/big.json", "src/a.rs"] and carry.whole_candidates(t, 1)[-1] == "src/c.rs"
    env = _fake_env({"/app/src/a.rs": "fn main() {}\n" * 40, "/tmp/big.json": "x" * 100_000, "/app/src/c.rs": "C-disk"})
    text, files, left_out = await carry.whole_block(t, env, 2000, after=2)
    assert files == ["src/a.rs"] and left_out == ["/tmp/big.json"]          # too large: never cut, left out
    assert text.startswith(carry.WHOLE_HEADER) and text.endswith("$ cat src/a.rs\n" + "fn main() {}\n" * 40)
    assert t.whole == {"src/a.rs"}
    snap = t.carry_block(4000)
    assert "A-old-part" not in snap and "C-snapshot" in snap and t.carried == {"src/a.rs", "src/c.rs", "/tmp/big.json"}
    assert await carry.whole_block(t, env, 0) == ("", [], [])
    t2 = RereadTracker()
    assert await carry.whole_block(t2, env, 2000) == ("", [], [])


def test_carried_size_enters_Lp_at_once_and_is_not_learned_twice():
    cfg = dict(mode="formula", Lp=2000, L0_mode="fixed", L0_fixed=12000, alpha=10, beta=50, m=1, summary_out=2000,
               g_prior=2000, learn_lp=True, learn_recent=3, reread_charge=False)
    ctl = LimitController(ControllerConfig(**cfg))
    ctl.on_prompt("s", "main", 14000.0, 12000.0)
    for p in (20000.0, 30000.0):
        ctl.on_prompt("s", "main", p, p - 2000)
    plain = ctl.on_prompt("s", "main", 14500.0, 12000.0, window_start_hint=True)      # window without carry
    s = ctl.session("s")
    assert s.lp_obs == [2500.0] and ctl.Lp(s) == 2500.0
    ctl.on_prompt("s", "main", 30000.0, 28000.0)
    ctl.note_carry("s", 40000)                                                          # 40K carried into the next window
    d = ctl.on_prompt("s", "main", 54500.0, 12000.0, window_start_hint=True)
    assert s.lp_obs == [2500.0, 2500.0] and ctl.Lp(s) == 42500.0                        # the limit follows at once
    assert d.limit > 54500 + 4 * 2000 and d.limit - plain.limit > 40000
    ctl.on_prompt("s", "main", 56500.0, 54500.0)
    ctl.note_carry("s", 0)                                                              # carry ends: Lp falls back
    ctl.on_prompt("s", "main", 14500.0, 12000.0, window_start_hint=True)
    assert ctl.Lp(s) == 2500.0
    assert LimitController(ControllerConfig(**cfg)).Lp(LimitController(ControllerConfig(**cfg)).session("x")) == 2000


def test_pick_container_follows_the_compose_project_to_the_main_service():
    from optlimit.envexec import pick_container
    def c(i, ip, project=None, service=None):
        return {"Id": i, "NetworkSettings": {"Networks": {"n": {"IPAddress": ip}} if ip else {}},
                "Config": {"Labels": ({"com.docker.compose.project": project, "com.docker.compose.service": service}
                                      if project else {})}}
    side = c("side", "172.20.0.2", "trial-a", "harbor-docker-egress-control-sidecar")
    main = c("main", "", "trial-a", "main")                    # shares the sidecar's network namespace: no address
    other = c("other", "172.21.0.2", "trial-b", "main")
    assert pick_container([other, main, side], "172.20.0.2") == "main"
    assert pick_container([other, main, side], "172.21.0.2") == "other"
    assert pick_container([c("plain", "172.17.0.3")], "172.17.0.3") == "plain"
    assert pick_container([other, main, side], "10.0.0.9") is None


@pytest.mark.asyncio
async def test_proxy_carries_a_file_whole_after_two_consecutive_reread_windows(tmp_path):
    seen, asked = [], []

    async def upstream(request):
        body = json.loads(await request.body())
        seen.append(body)
        n = sum(len(json.dumps(x)) for x in body["input"])
        return JSONResponse({"id": "r", "object": "response", "output": [],
                             "usage": {"input_tokens": 12000 + n, "input_tokens_details": {"cached_tokens": 12000},
                                       "output_tokens": 100, "total_tokens": 12100 + n}})

    disk = "fn main() { /* the whole file */ }\n" * 30

    async def env_run(ip, cmd):
        asked.append((ip, cmd))
        return (0, disk) if "src/a.rs" in cmd and "/app" in cmd else (1, "")

    cfg = ProxyConfig(trigger="off", log_path=str(tmp_path / "log.jsonl"), env_run=env_run,
                      controller=ControllerConfig(mode="formula", Lp=1700, L0_mode="fixed", L0_fixed=12000, alpha=10,
                                                  beta=50, m=1, summary_out=1700, g_prior=2000, carry_files=4000,
                                                  carry_whole=5000, learn_lp=True),
                      transport=httpx.ASGITransport(app=Starlette(routes=[Route("/v1/responses", upstream, methods=["POST"])])))
    app = create_app(cfg)
    task = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "port the service"}]}
    hs = [{"x-client-request-id": "T", "x-codex-window-id": f"T:{i}"} for i in range(4)]
    def read(cid):
        return [{"type": "custom_tool_call", "name": "exec", "call_id": cid,
                 "input": 'const r = await tools.exec_command({cmd:"sed -n 1,3p src/a.rs",workdir:"/app"});'},
                {"type": "custom_tool_call_output", "call_id": cid, "output": "A-part"}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        await c.post("/v1/responses", json={"model": "m", "input": [task] + read("r0")}, headers=hs[0])
        for w in (1, 2, 3):
            cmp_ = {"type": "compaction", "id": f"cmp_{w}", "encrypted_content": "x"}
            await c.post("/v1/responses", json={"model": "m", "input": [task, cmp_]}, headers=hs[w])
            await c.post("/v1/responses", json={"model": "m", "input": [task, cmp_] + read(f"r{w}")},
                         headers=hs[w])
    recs = [json.loads(l) for l in open(tmp_path / "log.jsonl")]
    starts = [r for r in recs if r.get("reread_window")]
    assert [r["reread_window"]["window"] for r in starts] == [0, 1, 2]
    assert "carry_whole" not in starts[0] and starts[1]["carry_whole"]["files"] == []      # one re-read window so far
    assert starts[2]["carry_whole"]["files"] == ["src/a.rs"] and starts[2]["carry"]["files"] == ["src/a.rs"]
    assert len(asked) == 1 and "/app" in asked[0][1]
    blocks = [b["input"][2]["content"][0]["text"] for b in seen[5:]]
    assert blocks[0] == blocks[1] and blocks[0].endswith("$ cat src/a.rs\n" + disk) and "A-part" not in blocks[0]
    s = app.state.controller.session("cx:T")
    assert s.carry_now == pytest.approx(len(blocks[0]) / 3.5)
    assert "A-part" in seen[3]["input"][2]["content"][0]["text"]            # window 2: still the snapshot of the read


# ---------------------------------------------------------------- Re-orientation (tests/test_reorient.py has the modes)
def test_reorient_v0_wording_and_config_validation():
    from optlimit.dialects import OpenAIResponses
    from optlimit.reorient import PARALLEL, REORIENT_MARKS, V0_MARK, reorient_block

    v0 = reorient_block("parallel_v0")                        # wording of the 2026-11-23 SWE-Marathon runs
    assert v0.startswith(V0_MARK) and "ONE step" in v0 and v0 != PARALLEL
    assert all(reorient_block(m).startswith(REORIENT_MARKS) for m in ("parallel", "sequential", "parallel_v0"))
    d = OpenAIResponses()                                     # the "already inserted" check knows the v0 mark too
    body = {"input": [{"type": "message", "role": "user", "content": "task"},
                      {"type": "compaction", "id": "cmp_1", "encrypted_content": "x"}]}
    assert d.inject_carry(body, v0) and not d.inject_carry(body, v0) and len(body["input"]) == 3
    with pytest.raises(ValueError):                           # a typo in a config fails at start-up
        ControllerConfig(reorient="paralel")


def test_tracker_measures_the_reorientation_phase():
    """Phase = model calls of a window before the first call that edits a file."""
    t = RereadTracker()
    t.observe("a", "exec", 'const r = await tools.exec_command({cmd:"cat src/a.rs"})'); t.close_turn(5)
    t.start_window()
    assert t.last_phase["turns"] == 1 and not t.last_phase["ended_by_edit"]
    # window 1: one call running three commands, a second read, then an edit, then another read
    t.observe("b", "exec", 'await tools.exec_command({cmd:"cat src/a.rs"}); await tools.exec_command({cmd:"cat src/b.rs"}); '
                          'await tools.exec_command({cmd:"cargo test"})'); t.close_turn(100)
    t.observe("c", "exec", 'await tools.exec_command({cmd:"sed -n 1,20p src/c.rs"})'); t.close_turn(40)
    t.observe("d", "exec", 'const patch = "*** Begin Patch\\n*** Update File: src/a.rs\\n"'); t.close_turn(30)
    t.observe("e", "exec", 'await tools.exec_command({cmd:"cat src/a.rs"})'); t.close_turn(20)
    t.close_turn(9)                                           # a model call without tool calls is not a phase turn
    w, turns, cost = t.start_window()
    assert (w, turns) == (1, 1)                               # the path-based count is unchanged by the phase fields
    assert t.last_phase == {"turns": 2, "calls": 2, "cost": 140, "ended_by_edit": True,
                            "first_calls": 1, "first_cmds": 3, "first_files": 2}
    assert t.phase_open and t.phase_turns == 0                # reset for the next window


def test_proxy_injects_the_reorientation_note(tmp_path):
    """reorient=parallel: the note sits behind the compaction item on every request of the window, once."""
    from optlimit.dialects import OpenAIResponses as ResponsesDialect
    from optlimit.reorient import REORIENT_MARK as MARK, reorient_block

    d = ResponsesDialect()
    note = reorient_block("parallel")
    body = {"input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]},
                      {"type": "compaction", "encrypted_content": "x"},
                      {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go on"}]}]}
    assert d.inject_carry(body, note)
    assert body["input"][2]["content"][0]["text"].startswith(MARK) and len(body["input"]) == 4
    assert not d.inject_carry(body, note) and len(body["input"]) == 4      # already there: not doubled
    # behind a carry block the note comes last (it refers to what is above)
    both = "\n\n".join(("[Carried over from before the context compaction] files ...", note))
    body2 = {"input": [{"type": "compaction", "encrypted_content": "x"}]}
    assert d.inject_carry(body2, both) and body2["input"][1]["content"][0]["text"].endswith("Then continue the work.")
    assert not d.inject_carry({"input": [{"type": "message", "role": "user", "content": "no compaction yet"}]}, note)


def test_refetch_premium_adds_the_rewrite_cost_of_refetched_tokens():
    """reread_premium: C grows by (alpha - 1) x W; the limit is the minimiser of (window cost + E) / (K - r)."""
    from dataclasses import replace as _replace

    base = dict(mode="formula", Lp=1700, L0_mode="fixed", L0_fixed=11500, alpha=10, beta=50, m=1, gamma=1.4,
                summary_out=1700, g_prior=2000, reread_productive=True)
    prod = LimitController(ControllerConfig(**base))
    prem = LimitController(ControllerConfig(**base, reread_premium=True))
    assert not prod.cfg.reread_premium                                    # off by default
    for c in (prod, prem):
        c.on_prompt("s", "main", 12000.0)
    assert prem.limit(prem.session("s"))[0] == prod.limit(prod.session("s"))[0]   # nothing observed yet: no premium
    # three windows whose re-read calls brought back 15K tokens each, in 2 calls (batched re-reads)
    for c in (prod, prem):
        s = c.session("s")
        s.rr_w_obs += [15000.0] * 3
        for _ in range(3):
            c.note_reread("s", 2, 1.0)
    s = prem.session("s")
    assert prem.refetch_premium(s) == 9 * 15000 and prod.refetch_premium(prod.session("s")) == 0
    r = (3.5 + 6) / 4

    def best(E):
        p0 = _replace(prem.params(s), summary_fixed=0.0)
        def h(L):
            K = cm.turns_per_window(L, p0)
            return (cm.f(L, p0) * K + E + 50 * 300.0 * K) / (K - r) if K > r + 0.5 else float("inf")
        return min(range(15000, 150000, 50), key=h)

    assert abs(prod.limit(prod.session("s"))[0] - best(0)) < 200
    assert abs(prem.limit(s)[0] - best(9 * 15000)) < 200
    assert prem.limit(s)[0] > prod.limit(prod.session("s"))[0] + 2000     # the premium keeps the limit up


def test_controller_records_refetched_tokens_per_window():
    c = LimitController(ControllerConfig(mode="formula", Lp=1700, L0_mode="fixed", L0_fixed=11500, alpha=10, beta=50,
                                         g_prior=2000, reread_productive=True, reread_premium=True))
    c.on_prompt("s", "main", 12000.0); c.on_prompt("s", "main", 30000.0)
    c.on_prompt("s", "main", 13000.0, window_start_hint=True)             # window 1 starts: window 0 is not recorded
    assert c.session("s").rr_w_obs == []
    c.note_reread_growth("s"); c.on_prompt("s", "main", 21000.0)          # +8K comes from a re-read call
    c.on_prompt("s", "main", 23000.0)                                     # ordinary growth
    c.note_reread_growth("s"); c.on_prompt("s", "main", 27000.0)          # +4K from a re-read call
    c.on_prompt("s", "main", 13500.0, window_start_hint=True)
    assert c.session("s").rr_w_obs == [12000.0]
    assert c.refetch_premium(c.session("s")) == 9 * 12000


@pytest.mark.asyncio
async def test_proxy_adds_the_reorientation_note_in_fixed_mode_too(tmp_path):
    """Through the proxy: no carry-over, fixed limit; the note is in every request of a window after a compaction."""
    from optlimit.reorient import REORIENT_MARK as MARK
    seen = []

    async def upstream(request):
        body = json.loads(await request.body())
        seen.append(body)
        n = sum(len(json.dumps(x)) for x in body["input"])
        return JSONResponse({"id": "r", "object": "response", "output": [],
                             "usage": {"input_tokens": 12000 + n, "input_tokens_details": {"cached_tokens": 12000},
                                       "output_tokens": 100, "total_tokens": 12100 + n}})

    cfg = ProxyConfig(trigger="off", log_path=str(tmp_path / "log.jsonl"),
                      controller=ControllerConfig(mode="fixed", fixed_limit=80000, reorient="parallel"),
                      transport=httpx.ASGITransport(app=Starlette(routes=[Route("/v1/responses", upstream, methods=["POST"])])))
    app = create_app(cfg)
    task = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "port the service"}]}
    h1, h2 = ({"x-client-request-id": "T", "x-codex-window-id": f"T:{i}"} for i in range(2))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        w0 = [task] + _exec("a", "cat src/a.rs", "A-contents")
        await c.post("/v1/responses", json={"model": "m", "input": w0}, headers=h1)
        await c.post("/v1/responses", json={"model": "m", "input": w0 + [{"type": "compaction_trigger"}]}, headers=h1)
        cmp1 = {"type": "compaction", "id": "cmp_1", "encrypted_content": "x"}
        await c.post("/v1/responses", json={"model": "m", "input": [task, cmp1]}, headers=h2)      # window 1 starts
        w1 = [task, cmp1] + _exec("b", "cat src/a.rs; cat src/b.rs", "A B")
        await c.post("/v1/responses", json={"model": "m", "input": w1}, headers=h2)
    assert not any(MARK in json.dumps(b) for b in seen[:2])        # nothing before the first compaction
    for body in seen[2:]:
        assert body["input"][1]["type"] == "compaction"
        assert body["input"][2]["content"][0]["text"].startswith(MARK)
        assert sum(MARK in json.dumps(x) for x in body["input"]) == 1
    assert seen[2]["input"][2] == seen[3]["input"][2]
    recs = [json.loads(l) for l in open(tmp_path / "log.jsonl")]
    assert recs[2]["reorient"] == "parallel" and recs[2]["carry_injected"] is True
    assert "phase_window" not in recs[2]                             # window 0 is not a re-orientation phase
