"""Claude Code / Codex adapters (optlimit.adapters.harbor_installed) and their config generator."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from optlimit.adapters.harbor_installed import OptlimitClaudeCode, claude_code_compaction_env

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("limit,window,expect", [
    (87_000, None, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "87"}),
    (40_000, 100_000, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40"}),
    (40_500, 100_000, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "41"}),
    (150_000, None, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}),
    (None, 2_000_000, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"}),
    (None, None, {}),
])
def test_claude_code_threshold_env(limit, window, expect):
    assert claude_code_compaction_env(limit, window) == expect


def test_claude_code_agent_env_and_resume_flag(tmp_path):
    a = OptlimitClaudeCode(logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5",
                           compaction={"limit": 60_000}, extra_env={"FOO": "1"})
    assert a._extra_env == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000",
                            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60", "FOO": "1"}
    assert "--continue" not in a.build_cli_flags()
    a._resume = True
    assert a.build_cli_flags().endswith("--continue")
    with pytest.raises(ValueError):
        OptlimitClaudeCode(logs_dir=tmp_path, model_name="m", compaction={"limt": 1})


def _gen(tmp_path, *args):
    out = tmp_path / "job.yaml"
    subprocess.run([sys.executable, str(ROOT / "configs" / "make_harness_config.py"), *args, "--out", str(out),
                    "--tasks", "seismo_cross_correlation_lag__01"], check=True, capture_output=True, cwd=ROOT)
    text = out.read_text()
    header = {ln.split(":", 1)[0][2:]: ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("# ")}
    return yaml.safe_load(text), header


def _validate(cfg):
    from harbor.models.job.config import JobConfig
    return JobConfig.model_validate(cfg)


def test_static_claude_code_config(tmp_path):
    cfg, header = _gen(tmp_path, "--harness", "claude-code", "--model", "anthropic/claude-sonnet-5-5", "--limit", "87k")
    agent = cfg["agents"][0]
    assert agent["import_path"].endswith(":OptlimitClaudeCode")
    assert agent["kwargs"]["compaction"] == {"limit": 87_000}
    assert "proxy" not in header and header["env"] == "HB_CONTINUE_MODE=same_conversation"
    _validate(cfg)


def test_balance_codex_config_has_proxy_band(tmp_path):
    cfg, header = _gen(tmp_path, "--harness", "codex", "--model", "openai/gpt-6-luna",
                       "--balance", "codex-luna-hub", "--prices", "gpt-6-luna", "--learn")
    proxy = json.loads(header["proxy"])
    assert proxy["trigger"] == "usage" and proxy["spec"]["mode"] == "balance"
    assert proxy["spec"]["learn_lp"] and proxy["spec"]["prices"] == "gpt-6-luna"
    native = cfg["agents"][0]["kwargs"]["compaction"]["limit"]
    # the harness's own trigger sits between the proxy's low cap and its compaction signal
    assert proxy["low_cap"] < native < proxy["high"]
    assert proxy["spec"]["limit_max"] < native
    _validate(cfg)


def test_codex_flags_and_resume_rewrite(tmp_path):
    import asyncio
    from optlimit.adapters.harbor_installed import OptlimitCodex, codex_compaction_flags
    assert codex_compaction_flags(46_000) == ["-c model_auto_compact_token_limit=46000"]
    assert codex_compaction_flags(None, "body_after_prefix") == \
        ["-c 'model_auto_compact_token_limit_scope=\"body_after_prefix\"'"]
    with pytest.raises(ValueError):
        codex_compaction_flags(None, "everything")

    a = OptlimitCodex(logs_dir=tmp_path, model_name="openai/gpt-6-luna", compaction={"limit": 46_000})
    assert "-c model_auto_compact_token_limit=46000" in a.build_cli_flags()
    seen = []

    async def fake_exec(environment, command, env=None, cwd=None, timeout_sec=None):
        seen.append(command)
    a._exec = fake_exec
    cmd = "codex exec --json --model gpt-6-luna -c model_auto_compact_token_limit=46000 -- 'fix it -- now' 2>&1"
    asyncio.run(a.exec_as_agent(None, cmd))
    assert seen[-1] == cmd                       # first run: untouched
    a._resume = True
    asyncio.run(a.exec_as_agent(None, cmd))
    assert "cp -R /logs/agent/sessions \"$CODEX_HOME/sessions\"" in seen[-1]
    assert "-c model_auto_compact_token_limit=46000 resume --last -- 'fix it -- now'" in seen[-1]
    asyncio.run(a.exec_as_agent(None, "mkdir -p /logs/agent"))
    assert seen[-1] == "mkdir -p /logs/agent"    # other commands untouched


def test_proxy_turns_writes_token_turns_tables(tmp_path):
    """Proxy log -> analysis/token_turns.py format (one trial per session), usable by replay.py."""
    import csv
    def rec(kind, P, cached, out, ws=False, compact=False):
        return {"session": "cx:T1", "kind": kind, "action": "forwarded", "status": 200, "prompt": P,
                "cached": cached, "cache_write": P - cached, "uncached": 0, "output": out,
                "decision": {"window_start": ws, "compact_now": compact, "window_idx": int(ws)},
                "root_preview": "task"}
    log = tmp_path / "job.proxy.jsonl"
    rows = [rec("main", 10_000, 0, 100), rec("main", 12_000, 10_000, 100), rec("main", 15_000, 12_000, 100, compact=True),
            rec("summary", 15_500, 9_000, 800), rec("main", 6_000, 4_000, 100, ws=True), rec("main", 7_000, 6_000, 100)]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    subprocess.run([sys.executable, str(ROOT / "analysis" / "proxy_turns.py"), str(log), "--prices", "gpt-6-luna",
                    "--job-dir", str(tmp_path / "jobx"), "--condition", "native"], check=True, capture_output=True)
    sess = list(csv.DictReader(open(tmp_path / "job.proxy.sessions.csv")))
    assert sess[0]["main_calls"] == "5" and sess[0]["summary_calls"] == "1" and sess[0]["compactions"] == "1"
    turns = list(csv.DictReader(open(tmp_path / "jobx" / "token_turns" / "turns.csv")))
    main = [t for t in turns if t["kind"] == "main"]
    assert [t["turn"] for t in main] == ["1", "2", "3", "4", "5"]
    assert [t["compaction"] for t in main] == ["0", "0", "0", "1", "0"]
    assert main[1]["prompt_delta"] == "2000" and main[3]["prompt_delta"] == "-9000"
    assert [t["kind"] for t in turns].count("summarization") == 1
    summ = list(csv.DictReader(open(tmp_path / "jobx" / "token_turns" / "summary.csv")))
    assert summ[0]["condition"] == "native" and summ[0]["trial"] == "cx:T1"


def test_cli_run_marks_completion_for_continue_loop():
    from harbor.models.agent.context import AgentContext
    from optlimit.adapters.harbor_installed import _mark_completed
    ctx = AgentContext()
    _mark_completed(ctx)
    assert ctx.metadata["termination_reason"] == "confirmed_task_complete"
    ctx = AgentContext(metadata={"termination_reason": "timeout"})
    _mark_completed(ctx)
    assert ctx.metadata["termination_reason"] == "timeout"


def test_claude_code_dynamic_config_uses_error_trigger_and_min_window(tmp_path):
    cfg, header = _gen(tmp_path, "--harness", "claude-code", "--model", "anthropic/claude-haiku-5-5",
                       "--balance", "claude-code-hub", "--prices", "claude-haiku-5-5")
    proxy = json.loads(header["proxy"])
    assert proxy["trigger"] == "error"                 # Claude Code ignores usage; reacts to a 400
    assert proxy["spec"]["min_window_turns"] == 4      # stay clear of its 3-turn thrash guard
    cfg, header = _gen(tmp_path, "--harness", "codex", "--model", "openai/gpt-6-luna", "--proxy-fixed", "40k")
    assert json.loads(header["proxy"])["trigger"] == "usage"
    _validate(cfg)
