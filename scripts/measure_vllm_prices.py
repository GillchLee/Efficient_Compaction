#!/usr/bin/env python3
"""Measure a vLLM server's cost ratios and write them as a configs/prices.json row.

    python scripts/measure_vllm_prices.py --api-base http://HOST:8000/v1 --model Qwen3-32B \
        --name vllm-qwen3-32b [--sizes 4000 16000 32000] [--repeat 2] [--output-tokens 256]

The compaction formula only uses price ratios (alpha = write/read, beta = output/read), so a
local model is priced in *seconds per million tokens* measured on the serving box:
  write   = uncached prefill time per prompt token      (first call with a fresh prompt)
  read    = prefill time per prompt token when the prefix is cached (same prompt again;
            needs vLLM --enable-prefix-caching, the default in recent versions)
  output  = decode time per generated token
The row is written under models[--name] with the raw measurements in _source. It also reports
whether the server returns usage.prompt_tokens_details.cached_tokens, which OptlimitTerminus2
and token_turns.py use for cache read; without it every prompt counts as a cache write.
Only the standard library is used (no API key needed unless vLLM was started with --api-key).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def chat(api_base: str, model: str, prompt: str, max_tokens: int, key: str) -> tuple[float, dict]:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0}).encode()
    req = urllib.request.Request(api_base.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.load(r)
    return time.perf_counter() - t0, out.get("usage") or {}


def words(n_tokens: int, rng: random.Random) -> str:
    # ~1 token per short lowercase word on most tokenizers; the fit uses the reported prompt_tokens
    return " ".join("".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(4)) for _ in range(n_tokens))


def slope(xs: list[float], ys: list[float]) -> float:
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else ys[-1] / xs[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--api-base", default=os.environ.get("HOSTED_VLLM_API_BASE"), help="http://host:8000/v1")
    ap.add_argument("--model", required=True, help="served model name (without hosted_vllm/)")
    ap.add_argument("--name", required=True, help="prices.json row name, e.g. vllm-qwen3-32b")
    ap.add_argument("--sizes", type=int, nargs="+", default=[4000, 16000, 32000], help="prompt sizes in tokens")
    ap.add_argument("--repeat", type=int, default=2, help="measurements per size")
    ap.add_argument("--output-tokens", type=int, default=256)
    ap.add_argument("--out", default=str(HERE / "configs" / "prices.json"))
    ap.add_argument("--dry-run", action="store_true", help="print the row, do not write")
    a = ap.parse_args()
    if not a.api_base:
        ap.error("--api-base or HOSTED_VLLM_API_BASE is required")
    key = os.environ.get("HOSTED_VLLM_API_KEY", "EMPTY")
    rng = random.Random(0)

    # warm-up (model load, CUDA graphs)
    chat(a.api_base, a.model, "hello", 1, key)

    n_write, t_write, n_read, t_read = [], [], [], []
    cached_seen = None
    for size in a.sizes:
        for _ in range(a.repeat):
            prompt = words(size, rng)
            dt, u = chat(a.api_base, a.model, prompt, 1, key)
            n_write.append(u.get("prompt_tokens", size)); t_write.append(dt)
            dt2, u2 = chat(a.api_base, a.model, prompt, 1, key)
            n_read.append(u2.get("prompt_tokens", size)); t_read.append(dt2)
            det = (u2.get("prompt_tokens_details") or {})
            cached_seen = det.get("cached_tokens") if det else cached_seen
            print(f"prompt {u.get('prompt_tokens')} tok: uncached {dt:.2f}s, repeat {dt2:.2f}s, "
                  f"cached_tokens={det.get('cached_tokens') if det else 'n/a'}", file=sys.stderr)
    # decode: small prompt, N output tokens; subtract the small-prompt prefill (~1-token call)
    t_small, _ = chat(a.api_base, a.model, "Write a long story about the sea.", 1, key)
    t_out, u_out = chat(a.api_base, a.model, "Write a long story about the sea.", a.output_tokens, key)
    n_out = u_out.get("completion_tokens", a.output_tokens) or a.output_tokens
    out_per_tok = max(t_out - t_small, 1e-6) / n_out
    # fixed per-call overhead is the intercept; the slopes are the per-token costs
    w = slope(n_write, t_write)
    r = max(slope(n_read, t_read), 1e-9)
    row = {
        "read": round(r * 1e6, 3), "write": round(w * 1e6, 3), "output": round(out_per_tok * 1e6, 3),
        "_units": "seconds per million tokens on the serving box (ratios are what the formula uses)",
        "_source": f"scripts/measure_vllm_prices.py {time.strftime('%Y-%m-%d %H:%M')} {a.api_base} model={a.model} "
                   f"sizes={a.sizes} repeat={a.repeat}: prefill uncached {w*1e3:.3f} ms/tok, cached {r*1e3:.3f} ms/tok, "
                   f"decode {out_per_tok*1e3:.1f} ms/tok ({n_out} tokens); cached_tokens reported: {cached_seen is not None}",
    }
    print(json.dumps({a.name: row}, indent=2))
    print(f"alpha = write/read = {row['write']/row['read']:.1f}, beta = output/read = {row['output']/row['read']:.1f}", file=sys.stderr)
    if cached_seen is None:
        print("WARNING: usage.prompt_tokens_details.cached_tokens not returned: start vLLM with "
              "--enable-prefix-caching (cache read cannot be observed otherwise)", file=sys.stderr)
    if a.dry_run:
        return 0
    path = Path(a.out)
    data = json.loads(path.read_text()) if path.exists() else {"models": {}}
    data["models"][a.name] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path} models[{a.name}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
