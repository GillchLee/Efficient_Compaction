"""make_config.py: option recording (`# env:`), vLLM fields, defaults unchanged."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent
MC = HERE / "configs" / "make_config.py"


def run(tmp_path, *args):
    out = tmp_path / "cfg.yaml"
    subprocess.run([sys.executable, str(MC), *args, "--tasks", "2048", "--out", str(out)], check=True,
                   capture_output=True, cwd=HERE)
    text = out.read_text()
    return text, yaml.safe_load(text)


def test_default_has_no_env_line(tmp_path):
    text, cfg = run(tmp_path, "--model", "openai/gpt-6-luna", "--limit", "60000")
    assert "# env:" not in text
    assert cfg["agents"][0]["kwargs"]["optlimit"] == {"mode": "fixed", "fixed_limit": 60000, "lookahead": 0}
    assert "api_base" not in cfg["agents"][0]["kwargs"] and "model_info" not in cfg["agents"][0]["kwargs"]


def test_env_line_and_vllm_fields(tmp_path):
    text, cfg = run(tmp_path, "--model", "hosted_vllm/Qwen3-32B", "--api-base", "http://h:8000/v1",
                    "--context-window", "131072", "--formula", "terminus2-gpt-6-luna-1.0", "--prices", "gpt-6-luna",
                    "--verifier-feedback", "false", "--continue-mode", "phases")
    assert "# env: HB_VERIFIER_FEEDBACK=false HB_CONTINUE_MODE=\n" in text
    k = cfg["agents"][0]["kwargs"]
    assert k["api_base"] == "http://h:8000/v1"
    assert k["model_info"]["max_input_tokens"] == 131072 and k["model_info"]["max_output_tokens"] == 16384
    assert k["optlimit"]["limit_max"] == 131072 - 16384 - 8000


def test_hosted_vllm_requires_context(tmp_path):
    r = subprocess.run([sys.executable, str(MC), "--model", "hosted_vllm/x", "--limit", "1000", "--tasks", "2048",
                        "--out", str(tmp_path / "c.yaml")], capture_output=True, text=True, cwd=HERE)
    assert r.returncode != 0 and "context-window" in r.stderr
