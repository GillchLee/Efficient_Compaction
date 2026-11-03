import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))
import token_turns  # noqa: E402


def _agent_step(step_id, ts, prompt, cached, completion, write=None, cmd=True):
    s = {"step_id": step_id, "timestamp": ts, "source": "agent", "message": "ok",
         "metrics": {"prompt_tokens": prompt, "cached_tokens": cached, "completion_tokens": completion,
                     "cost_usd": 0.01}}
    if write is not None:
        s["metrics"]["extra"] = {"cache_creation_tokens": write}
    if cmd:
        s["tool_calls"] = [{"function_name": "bash_command", "arguments": {"keystrokes": "ls\n"}}]
    return s


def _make_job(tmp_path: Path) -> Path:
    job = tmp_path / "sonnet_l80k"
    trial = job / "mytask__01__abc1234"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (job / "config.json").write_text(json.dumps({"agents": [{
        "import_path": "optlimit.harbor_agent:OptlimitTerminus2", "model_name": "anthropic/claude-sonnet-5-5",
        "kwargs": {"optlimit": {"mode": "fixed", "fixed_limit": 80000}}}]}))
    steps = [
        {"step_id": 1, "timestamp": "2026-11-03T00:00:00Z", "source": "user", "message": "Do the task."},
        _agent_step(2, "2026-11-03T00:00:10Z", 1000, 0, 50, write=1000),
        _agent_step(3, "2026-11-03T00:00:20Z", 1500, 1000, 60, write=500),
        {"step_id": 4, "timestamp": "2026-11-03T00:00:25Z", "source": "system",
         "message": "Context summarization 1: handed off"},
        _agent_step(5, "2026-11-03T00:00:30Z", 700, 0, 40, write=700, cmd=False),
        {"step_id": 6, "timestamp": "2026-11-03T00:00:35Z", "source": "user",
         "message": "Your submitted solution did not pass verification. Keep working."},
        _agent_step(7, "2026-11-03T00:00:40Z", 1200, 700, 30, write=500),
        {"step_id": 8, "timestamp": "2026-11-03T00:00:41Z", "source": "agent", "is_copied_context": True,
         "metrics": {"prompt_tokens": 99999}},
    ]
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}))
    (agent / "trajectory.summarization-1-summary.json").write_text(json.dumps({"steps": [
        _agent_step(1, "2026-11-03T00:00:22Z", 1600, 0, 300, write=1600, cmd=False)]}))
    (agent / "optlimit_decisions.jsonl").write_text(
        json.dumps({"turn_idx": 2, "limit": 80000, "g_hat": 500, "compact_now": True}) + "\n")
    (trial / "interim_verifier.jsonl").write_text(
        json.dumps({"submission": 1, "timestamp": "2026-11-03T00:00:33+00:00", "remaining_sec": 100,
                    "status": "ok", "reward": 0.5, "passed": False}) + "\n")
    (trial / "result.json").write_text(json.dumps({
        "verifier_result": {"rewards": {"reward": 0.5}},
        "agent_result": {"metadata": {"continue_until_timeout_phases": 1, "first_completion_elapsed_sec": 30}},
        "exception_info": {"exception_type": "AgentTimeoutError"}}))
    return job


def test_collect_turns_summary_submissions(tmp_path):
    job = _make_job(tmp_path)
    turns, summaries, subs = token_turns.collect(job)
    main = [r for r in turns if r["kind"] == "main"]
    assert [r["turn"] for r in main] == [1, 2, 3, 4]
    assert [r["prompt_delta"] for r in main] == [1000, 500, -800, 500]
    assert [r["compaction"] for r in main] == [0, 0, 1, 0]
    assert [r["rejected"] for r in main] == [0, 0, 0, 1]
    assert [r["phase"] for r in main] == [0, 0, 0, 1]
    assert [r["has_cmd"] for r in main] == [1, 1, 0, 1]
    assert [r["cache_write"] for r in main] == [1000, 500, 700, 500]
    assert main[1]["ol_compact"] == 1 and main[1]["ol_limit"] == 80000 and main[0]["ol_compact"] == ""
    summ = [r for r in turns if r["kind"] == "summarization"]
    assert len(summ) == 1 and summ[0]["file"] == "trajectory.summarization-1-summary.json"

    s = summaries[0]
    assert s["model"] == "claude-sonnet-5-5" and s["condition"] == "l80k"
    assert s["reward"] == 0.5 and s["end"] == "timeout" and s["phases"] == 1
    assert s["compactions"] == 1 and s["turns"] == 4 and s["cmd_turns"] == 3
    assert s["summ_calls"] == 1 and s["summ_prompt_tokens"] == 1600
    assert s["first_completion_min"] == 0.5

    assert len(subs) == 1 and subs[0]["turn"] == 3 and subs[0]["reward"] == 0.5


def test_usage_log_fills_cache_write(tmp_path):
    job = _make_job(tmp_path)
    # strip the recorded writes, then recover them from a usage log (older runs)
    traj = job / "mytask__01__abc1234" / "agent" / "trajectory.json"
    data = json.loads(traj.read_text())
    for s in data["steps"]:
        if s.get("metrics"):
            s["metrics"].pop("extra", None)
    traj.write_text(json.dumps(data))
    import hashlib
    key = hashlib.sha1(b"Do the task.").hexdigest()[:12]
    base = token_turns._ts("2026-11-03T00:00:00Z")
    recs = [
        {"ts": base + 9, "prompt_tokens": 1000, "cache_write": 1000, "conv_key": key},
        {"ts": base + 19, "prompt_tokens": 1500, "cache_write": 500, "conv_key": key},
        {"ts": base + 21, "prompt_tokens": 1600, "cache_write": 1600, "conv_key": "other"},
        {"ts": base + 29, "prompt_tokens": 700, "cache_write": 700, "conv_key": key},
        {"ts": base + 39, "prompt_tokens": 1200, "cache_write": 500, "conv_key": key},
    ]
    usage = tmp_path / "usage.jsonl"
    usage.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    turns, summaries, _ = token_turns.collect(job, usage)
    main = [r for r in turns if r["kind"] == "main"]
    assert [r["cache_write"] for r in main] == [1000, 500, 700, 500]
    assert [r["uncached"] for r in main] == [0, 0, 0, 0]
    assert summaries[0]["summ_cache_write"] == 1600


def test_job_condition_from_name_and_config(tmp_path):
    for name, cfg, want in [
        ("lhtb-luna-ctx80k", {"agents": [{"model_name": "openai/gpt-6-luna", "kwargs": {}}]}, ("gpt-6-luna", "l80k")),
        ("sc_sonnet_l80k_gdal", {"agents": [{"model_name": "anthropic/claude-sonnet-5-5", "kwargs": {}}]}, ("claude-sonnet-5-5", "l80k")),
        ("luna_ctx80k_rerun_law433", {"agents": [{"model_name": "openai/gpt-6-luna", "kwargs": {}}]}, ("gpt-6-luna", "l80k")),
        ("x_native", {"agents": [{"model_name": "a/b", "kwargs": {"proactive_summarization_threshold": 900000}}]}, ("b", "native")),
        ("thr", {"agents": [{"model_name": "a/b", "kwargs": {"proactive_summarization_threshold": 842000}}]}, ("b", "thr842k")),
        ("f", {"agents": [{"model_name": "a/b", "kwargs": {"optlimit": {"mode": "formula", "prior": "p"}}}]}, ("b", "formula:p")),
    ]:
        d = tmp_path / name
        d.mkdir()
        (d / "config.json").write_text(json.dumps(cfg))
        assert token_turns.job_condition(d) == want
