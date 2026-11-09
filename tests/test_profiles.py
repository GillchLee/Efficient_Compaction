"""Environment profiles (one venv per Harbor base) and the benchmarks that need stock Harbor:
presets carry `harbor:`, the generators write a `# harbor:` line, run_job.sh picks the venv."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "configs"))
sys.path.insert(0, str(HERE))

from _common import DEFAULT_HARBOR, HARBOR_PROFILES, load_benchmarks  # noqa: E402
from optlimit import harnesses  # noqa: E402

MC = HERE / "configs" / "make_config.py"


def run_mc(tmp_path, *args, check=True):
    out = tmp_path / "cfg.yaml"
    r = subprocess.run([sys.executable, str(MC), *args, "--out", str(out)], capture_output=True, text=True, cwd=HERE)
    if check:
        assert r.returncode == 0, r.stderr
        text = out.read_text()
        return text, yaml.safe_load(text)
    return r


def test_presets_name_a_known_profile():
    b = load_benchmarks()
    for name, preset in b.items():
        assert str(preset.get("harbor", DEFAULT_HARBOR)) in HARBOR_PROFILES, name
    assert b["swe_marathon"]["harbor"] == "0.25" and b["frontier_swe_v2"]["harbor"] == "0.25"
    assert b["lhtb2"]["harbor"] == b["tb4"]["harbor"] == "lhtb-fork"


def test_swe_marathon_claude_code_config(tmp_path):
    text, cfg = run_mc(tmp_path, "--harness", "claude-code", "--model", "anthropic/claude-haiku-5-5",
                       "--proxy-fixed", "30k", "--benchmark", "swe_marathon", "--tasks", "zstd-decoder", "--hours", "1")
    assert "# harbor: 0.25\n" in text and "# env: HB_CONTINUE_MODE=\n" in text
    agent = cfg["agents"][0]
    assert agent["kwargs"]["disallowed_tools"] == "WebSearch WebFetch"      # the benchmark's protocol
    assert agent["kwargs"]["compaction"] == harnesses.get("claude-code").native_band
    assert cfg["datasets"] == [{"path": "./data/swe-marathon/tasks", "task_names": ["zstd-decoder"]}]
    assert agent["override_timeout_sec"] == 3600


def test_terminus2_proxy_config(tmp_path):
    text, cfg = run_mc(tmp_path, "--harness", "terminus-2-proxy", "--model", "anthropic/claude-haiku-5-5",
                       "--proxy-fixed", "30k", "--benchmark", "frontier_swe_v2", "--tasks", "x", "--hours", "1")
    px = json.loads(next(ln for ln in text.splitlines() if ln.startswith("# proxy:")).split(":", 1)[1])
    assert px["trigger"] == "error" and px["spec"]["fixed_limit"] == 30000
    agent = cfg["agents"][0]
    assert agent["name"] == "terminus-2" and "import_path" not in agent
    assert agent["kwargs"]["enable_summarize"] is True and "compaction" not in agent["kwargs"]
    assert "# harbor: 0.25\n" in text


def test_default_profile_writes_no_harbor_line(tmp_path):
    text, _ = run_mc(tmp_path, "--harness", "claude-code", "--model", "anthropic/claude-haiku-5-5",
                     "--proxy-fixed", "40k", "--benchmark", "lhtb2", "--tasks", "x")
    assert "# harbor:" not in text
    text, _ = run_mc(tmp_path, "--harness", "claude-code", "--model", "anthropic/claude-haiku-5-5",
                     "--proxy-fixed", "40k", "--tasks-dir", "./somewhere", "--harbor", "0.25", "--tasks", "x")
    assert "# harbor: 0.25\n" in text


def test_in_process_terminus2_refuses_stock_harbor_benchmarks(tmp_path):
    r = run_mc(tmp_path, "--model", "anthropic/claude-haiku-5-5", "--limit", "80000", "--benchmark", "swe_marathon",
               "--tasks", "zstd-decoder", check=False)
    assert r.returncode != 0 and "terminus-2-proxy" in r.stderr


def test_terminus2_proxy_has_no_static_limit(tmp_path):
    r = run_mc(tmp_path, "--harness", "terminus-2-proxy", "--model", "anthropic/claude-haiku-5-5", "--limit", "40k",
               "--benchmark", "swe_marathon", "--tasks", "x", check=False)
    assert r.returncode != 0 and "--proxy-fixed" in r.stderr


def test_runners_and_setup_know_the_profiles():
    run_job = (HERE / "scripts" / "run_job.sh").read_text()
    assert ".venv-h25" in run_job and "HARBOR_EXTRA_ARGS" in run_job
    launcher = (HERE / "scripts" / "run_harness_job.sh").read_text()
    assert "--allow-agent-host" in launcher and "ANTHROPIC_API_BASE" in launcher
    setup = (HERE / "setup.sh").read_text()
    assert "--profile" in setup and "patches/lhtb-7ca0cac" in setup
    assert sorted(p.name for p in (HERE / "patches" / "lhtb-7ca0cac").glob("0*.patch"))[0].startswith("01-")


def test_installed_adapters_accept_compaction_kwarg_on_any_harbor():
    pytest.importorskip("harbor")
    from optlimit.adapters.harbor_installed import OptlimitClaudeCode
    parse = getattr(OptlimitClaudeCode, "parse_options", None)
    if parse is None:        # LHTB fork: kwargs are not validated against a model
        return
    assert parse({"compaction": {"window": 200000}}) is not None
