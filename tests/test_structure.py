"""The three axes (policy / harness / benchmark) stay independent: harness profiles, benchmark
presets, make_config delegation and the single run entry point."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "configs"))
sys.path.insert(0, str(HERE))

from _common import dataset_spec, load_benchmarks  # noqa: E402
from optlimit import dialects, harnesses  # noqa: E402

MC = HERE / "configs" / "make_config.py"
PRIORS = json.loads((HERE / "configs" / "priors.json").read_text())["harness"]


def make(tmp_path, *args):
    out = tmp_path / "cfg.yaml"
    subprocess.run([sys.executable, str(MC), *args, "--out", str(out)], check=True, capture_output=True, cwd=HERE)
    text = out.read_text()
    return text, yaml.safe_load(text)


def proxy_line(text):
    line = next(ln for ln in text.splitlines() if ln.startswith("# proxy:"))
    return json.loads(line.split(":", 1)[1])


@pytest.mark.parametrize("name", sorted(harnesses.PROFILES))
def test_profile_is_consistent(name):
    p = harnesses.get(name)
    assert p.default_prior in PRIORS
    if p.connection == "proxy":
        assert p.trigger in ("usage", "error")
        assert p.band["low_cap"] < p.band["limit_max"] < p.band["high"]
        # the harness's own threshold must sit inside the band so only the proxy decides
        native = next(iter(p.native_band.values()))
        assert p.band["low_cap"] < native < p.band["high"]
        assert p.summary_markers and p.window_start_prefixes
    else:
        assert p.connection == "in-process"


def test_dialects_read_profiles():
    for p in harnesses.PROFILES.values():
        assert set(p.summary_prefixes) <= set(dialects.SUMMARY_PREFIXES)
        assert set(p.window_start_prefixes) <= set(dialects.WINDOW_START_PREFIXES)


def test_unknown_harness():
    with pytest.raises(KeyError):
        harnesses.get("nope")


def test_benchmark_presets():
    b = load_benchmarks()
    assert {"lhtb1", "lhtb2", "tb4"} <= set(b)
    for name, preset in b.items():
        assert ("tasks_dir" in preset) != ("dataset" in preset), name
        assert preset.get("continue_mode") in ("same_conversation", "phases"), name
        assert float(preset["hours"]) > 0, name


def test_dataset_spec():
    assert dataset_spec("terminal-bench/terminal-bench@4.0.0") == {"name": "terminal-bench/terminal-bench", "ref": "4.0.0"}
    assert dataset_spec("org/x") == {"name": "org/x", "ref": "latest"}
    assert dataset_spec("legacy@1.0") == {"name": "legacy", "version": "1.0"}


def test_tb4_terminus(tmp_path):
    text, cfg = make(tmp_path, "--model", "openai/gpt-6-luna", "--limit", "60000", "--benchmark", "tb4",
                     "--tasks", "coq-block-bound")
    assert cfg["datasets"] == [{"name": "terminal-bench/terminal-bench", "ref": "4.0.0", "task_names": ["coq-block-bound"]}]
    assert cfg["agents"][0]["override_timeout_sec"] == 8 * 3600
    assert "# env: HB_CONTINUE_MODE=\n" in text
    assert "# proxy:" not in text


@pytest.mark.parametrize("harness,trigger,minwin", [("codex", "usage", 3), ("claude-code", "error", 4)])
def test_make_config_delegates_to_harness(tmp_path, harness, trigger, minwin):
    text, cfg = make(tmp_path, "--harness", harness, "--model", "openai/gpt-6-luna", "--balance",
                     "--prices", "gpt-6-luna", "--benchmark", "lhtb1", "--tasks", "x")
    prof = harnesses.get(harness)
    px = proxy_line(text)
    assert px["trigger"] == trigger and px["min_window_turns"] == minwin
    assert px["spec"]["mode"] == "balance" and px["spec"]["prior"] == prof.default_prior
    agent = cfg["agents"][0]
    assert agent["import_path"] == prof.agent["import_path"]
    assert agent["kwargs"] == {"compaction": prof.native_band}
    assert cfg["datasets"][0]["path"] == "./LHTB/tasks"
    assert "# run: bash scripts/run_job.sh" in text


def test_static_limit_has_no_proxy(tmp_path):
    text, cfg = make(tmp_path, "--harness", "codex", "--model", "openai/gpt-6-luna", "--limit", "40k", "--tasks", "x")
    assert "# proxy:" not in text
    assert cfg["agents"][0]["kwargs"] == {"compaction": {"limit": 40000}}


def test_run_job_hands_proxy_configs_to_launcher():
    s = (HERE / "scripts" / "run_job.sh").read_text()
    assert "exec bash scripts/run_harness_job.sh" in s and "OPTLIMIT_PROXY_ACTIVE" in s
    assert "OPTLIMIT_PROXY_ACTIVE=1 bash scripts/run_job.sh" in (HERE / "scripts" / "run_harness_job.sh").read_text()
