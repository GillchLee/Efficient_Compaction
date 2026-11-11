"""Run the optlimit proxy (optlimit/proxy.py) in front of a harness that compacts on its own
(Claude Code, Codex): it records every call's usage, runs the controller, and makes the
harness compact at the controller's moment through the harness's native mechanism.

    python -m optlimit.serve --port 8300 --trigger usage \
        --spec '{"mode": "balance", "prior": "claude-code", "prices": "claude-sonnet-5-5",
                 "learn_lp": true, "learn_summary_out": true, "learn_gamma": true}' \
        --log jobs/<job>/optlimit_proxy.jsonl

Point the harness at it: Claude Code ANTHROPIC_BASE_URL=http://HOST:8300, Codex
OPENAI_BASE_URL=http://HOST:8300/v1 (scripts/run_harness_job.sh does this for Harbor jobs).
Needs the optional dependencies: pip install -e '.[proxy]'.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def build_config(a):
    from .controller import config_from_spec
    from .proxy import ProxyConfig

    spec = json.loads(a.spec) if a.spec else {}
    if a.spec_file:
        with open(a.spec_file) as f:
            spec = {**json.load(f), **spec}
    return ProxyConfig(
        anthropic_upstream=a.anthropic_upstream, openai_upstream=a.openai_upstream,
        trigger=a.trigger, signal_high=a.signal_high, signal_low_cap=a.signal_low_cap,
        log_path=a.log, dump_dir=a.dump_dir,
        inject_anthropic_key=os.environ.get("ANTHROPIC_API_KEY") if a.inject_keys else None,
        inject_openai_key=os.environ.get("OPENAI_API_KEY") if a.inject_keys else None,
        model_rewrite=dict(kv.split("=", 1) for kv in a.model_rewrite),
        min_window_turns=a.min_window_turns,
        attach_orphan_summaries=a.attach_orphan_summaries,
        controller=config_from_spec(spec),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--trigger", choices=["usage", "error", "off"], default="usage",
                    help="usage: rewrite the reported usage; error: answer with a context-length error; "
                         "off: observe only")
    ap.add_argument("--spec", help="controller spec as JSON (the optlimit: block of a job config)")
    ap.add_argument("--spec-file", help="the same spec from a JSON file (--spec keys win)")
    ap.add_argument("--log", help="per-request JSONL log (usage, decision, reported prompt)")
    ap.add_argument("--dump-dir", help="per-request text dumps (classification debugging)")
    ap.add_argument("--signal-high", type=int, default=950_000,
                    help="prompt size reported when the controller says compact (above the harness threshold)")
    ap.add_argument("--signal-low-cap", type=int, default=50_000,
                    help="prompt size reported at most otherwise (below the harness threshold)")
    ap.add_argument("--min-window-turns", type=int, default=0,
                    help="hold a decided compaction until the window has this many main turns (harness guard)")
    ap.add_argument("--attach-orphan-summaries", action="store_true",
                    help="charge harness compaction calls that open a new message root (terminus-2 "
                         "questions subagent) to the session mid-compaction, so learned O_s/gamma see them")
    ap.add_argument("--anthropic-upstream", default="https://api.anthropic.com")
    ap.add_argument("--openai-upstream", default="https://api.openai.com")
    ap.add_argument("--model-rewrite", action="append", default=[], metavar="SRC=DST",
                    help="rewrite body.model (Harbor's claude-code sends 'anthropic/<id>' to a custom base URL)")
    ap.add_argument("--inject-keys", action="store_true",
                    help="send this process's ANTHROPIC_API_KEY / OPENAI_API_KEY upstream instead of the client's")
    a = ap.parse_args(argv)

    import uvicorn

    from .proxy import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = build_config(a)
    logging.getLogger("optlimit").info("controller %s; trigger %s", cfg.controller, cfg.trigger)
    uvicorn.run(create_app(cfg), host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
