"""Transparent API proxy that (1) records per-request token usage, (2) runs the
LimitController on it, and (3) makes the harness compact at the chosen point by
its *native* mechanism:

  trigger=usage  rewrite the usage the harness sees.  Claude Code compares
                 input+cache_creation+cache_read of the last response with its
                 autocompact threshold; Codex compares usage.total_tokens with
                 model_auto_compact_token_limit.  Not yet -> report min(P, low_cap);
                 compact -> report signal_high.
  trigger=error  answer the next main-loop request with a context-length 400
                 (not forwarded).  Terminus-2 catches ContextLengthExceededError
                 and runs its summary/questions/answers handoff; Claude Code and
                 OpenHands also compact on prompt-too-long.
  trigger=off    observe only (shadow): log usage and what the controller would do.

Everything else is relayed byte-for-byte (streaming included).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import dialects as D
from .controller import ControllerConfig, LimitController

logger = logging.getLogger("optlimit")

_STRIP_REQ = {"host", "content-length", "connection", "accept-encoding"}
_STRIP_RESP = {"content-length", "content-encoding", "transfer-encoding", "connection"}


@dataclass
class ProxyConfig:
    anthropic_upstream: str = "https://api.anthropic.com"
    openai_upstream: str = "https://api.openai.com"
    trigger: str = "usage"              # usage | error | off
    signal_high: int = 950_000
    signal_low_cap: int = 50_000
    error_limit_tokens: int = 1_000_000  # number quoted in the synthetic context error
    max_consecutive_rejects: int = 3     # error trigger: fail open if the harness ignores the error
    # Harness guard: hold a decided compaction until the window has lasted this many main turns
    # (Claude Code aborts with "Autocompact is thrashing" after 3 compactions within 3 turns of each
    # other). While a compaction is pending the usage trigger keeps reporting the high signal, so a
    # harness that skipped one check compacts at its next one.
    min_window_turns: int = 0
    log_path: str | None = None
    dump_dir: str | None = None          # per-request text dumps (last user text, classification)
    dump_messages: bool = False          # also store the full message array (large)
    upstream_timeout: float = 3600.0
    inject_anthropic_key: str | None = None  # replace client x-api-key with this (keeps keys out of containers)
    inject_openai_key: str | None = None
    # API server-side compaction: when a request already asks the API to compact at a token
    # threshold (Anthropic context_management compact_* trigger, OpenAI Responses
    # context_management compaction compact_threshold), replace that threshold by the
    # controller's current L* (never below api_trigger_min, the API minimum).
    rewrite_api_compaction: bool = True
    api_trigger_min: int = 50_000
    extra_summary_prefixes: tuple[str, ...] = ()
    model_rewrite: dict = field(default_factory=dict)  # body.model SRC -> DST (harbor's codex strips 'vendor/' prefixes)
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    transport: object | None = None      # httpx transport override (tests)


class Recorder:
    def __init__(self, path: str | None):
        self.path = path
        self._f = open(path, "a", buffering=1) if path else None
        self.n = 0

    def write(self, rec: dict) -> None:
        self.n += 1
        if self._f:
            self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def rewrite_api_threshold(body: dict, threshold: int) -> bool:
    """Set the token threshold of an API-side compaction request in place; True if one was found."""
    found = False
    cm = body.get("context_management")
    edits = cm.get("edits") if isinstance(cm, dict) else cm if isinstance(cm, list) else None
    for e in edits or []:
        if not isinstance(e, dict):
            continue
        t = str(e.get("type", ""))
        if t.startswith("compact_") and isinstance(e.get("trigger"), dict) \
                and e["trigger"].get("type", "input_tokens") == "input_tokens":
            e["trigger"]["value"] = int(threshold)          # Anthropic compact_20260112
            found = True
        elif t == "compaction" and "compact_threshold" in e:
            e["compact_threshold"] = int(threshold)          # OpenAI Responses
            found = True
    return found


def create_app(cfg: ProxyConfig) -> Starlette:
    ctl = LimitController(cfg.controller)
    rec = Recorder(cfg.log_path)
    if cfg.extra_summary_prefixes:
        D.SUMMARY_PREFIXES = tuple(D.SUMMARY_PREFIXES) + tuple(cfg.extra_summary_prefixes)
    if cfg.dump_dir:
        os.makedirs(cfg.dump_dir, exist_ok=True)
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=cfg.upstream_timeout,
                                                     write=cfg.upstream_timeout, pool=30.0),
                               transport=cfg.transport)
    seq = {"n": 0}

    def upstream_for(path: str) -> str:
        d = D.detect(path)
        base = cfg.anthropic_upstream if d == "anthropic" else cfg.openai_upstream
        return base.rstrip("/") + path

    window_ids: dict[str, str] = {}
    rejects: dict[str, int] = {}

    def should_reject(key: str, kind: str) -> bool:
        """Error trigger: refuse the next main-loop request after a compact_now decision."""
        s = ctl.session(key)
        if kind != "main" or not ready(key):
            rejects[key] = 0
            return False
        if rejects.get(key, 0) >= cfg.max_consecutive_rejects:
            # the harness did not react to the error: fail open
            s.pending_compact = False
            rejects[key] = 0
            return False
        rejects[key] = rejects.get(key, 0) + 1
        s.expect_boundary = True
        return True

    def fwd_headers(request: Request, dialect_name: str | None) -> dict:
        h = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQ}
        # ask for an uncompressed body: httpx would otherwise add its own "gzip, deflate, ..."
        # and the SSE stream (relayed after parsing) would arrive compressed
        h["accept-encoding"] = "identity"
        if dialect_name == "anthropic" and cfg.inject_anthropic_key:
            h["x-api-key"] = cfg.inject_anthropic_key
            h.pop("authorization", None)
        elif dialect_name in ("openai", "responses") and cfg.inject_openai_key:
            h["authorization"] = "Bearer " + cfg.inject_openai_key
        return h

    def ready(key: str) -> bool:
        """A compaction is decided and the window is long enough to act on it now."""
        s = ctl.session(key)
        return s.pending_compact and s.window_turns >= cfg.min_window_turns

    def signal_for(decision, true_prompt: int, key: str | None = None) -> int | None:
        """usage-trigger: what prompt size to report."""
        if cfg.trigger != "usage" or decision is None:
            return None
        if ready(key) if key is not None else decision.compact_now:
            return cfg.signal_high
        return min(int(true_prompt), cfg.signal_low_cap)

    def dump(req_id: str, info: dict) -> None:
        if not cfg.dump_dir:
            return
        try:
            with open(Path(cfg.dump_dir) / f"{req_id}.json", "w") as f:
                json.dump(info, f, ensure_ascii=False)
        except Exception:
            logger.exception("dump failed")

    async def handle(request: Request) -> Response:
        t0 = time.monotonic()
        raw = await request.body()
        path = request.url.path
        dialect = D.get(path)
        seq["n"] += 1
        req_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{seq['n']:05d}"
        body = None
        meta: dict = {"id": req_id, "ts": time.time(), "path": path, "dialect": dialect.name if dialect else None}
        out_raw = raw
        if dialect is not None and request.method == "POST" and raw:
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    body = None
            except Exception:
                body = None
        if body is not None:
            hdrs = {k.lower(): v for k, v in request.headers.items()}
            key = dialect.session_key(body, hdrs)
            kind, ws_hint = dialect.classify(body)
            # Codex numbers its compaction windows: x-codex-window-id = "<thread>:<n>"; a change
            # of n inside a thread is an explicit new-window signal.
            wid = hdrs.get("x-codex-window-id")
            if wid and kind == "main":
                prev = window_ids.get(key)
                window_ids[key] = wid
                if prev is not None and prev != wid:
                    ws_hint = True
            meta.update({"session": key, "kind": kind, "window_start_hint": ws_hint,
                         "thread_headers": {k: v[:64] for k, v in hdrs.items()
                                            if k in ("x-codex-thread-id", "session_id", "conversation_id",
                                                     "x-client-request-id", "x-codex-window-id", "x-codex-parent-thread-id")},
                         "n_assistant": dialect.n_assistant(body), "model": body.get("model"),
                         "stream": dialect.is_stream(body), "root_preview": dialect.root_preview(body)[:160]})
            ctl.note_kind(key, kind)
            if cfg.dump_dir:
                info = dict(meta, last_user_text=dialect.last_user_text(body)[:4000])
                if cfg.dump_messages:
                    info["messages"] = dialect.messages(body)
                    info["system"] = body.get("system") or body.get("instructions")
                dump(req_id, info)
            # error trigger: refuse the next main turn after a compact decision
            if cfg.trigger == "error" and should_reject(key, kind):
                est = int(ctl.session(key).last_main_prompt or 0) + int(ctl.session(key).g.value)
                status, err = dialect.context_error(est, cfg.error_limit_tokens)
                meta.update({"action": "rejected", "status": status, "prompt_est": est,
                             "decision": ctl.session(key).last_decision.as_dict() if ctl.session(key).last_decision else None,
                             "latency_ms": round((time.monotonic() - t0) * 1000)})
                rec.write(meta)
                logger.info("reject %s session=%s turn=%d est=%d", req_id, key[:12], ctl.session(key).turn_idx, est)
                return JSONResponse(err, status_code=status)
            changed = dialect.prepare_request(body)
            if cfg.rewrite_api_compaction and kind == "main":
                L, _ = ctl.limit(ctl.session(key))
                if L is not None and rewrite_api_threshold(body, max(int(L), cfg.api_trigger_min)):
                    meta["api_compaction_threshold"] = max(int(L), cfg.api_trigger_min)
                    changed = True
            if cfg.model_rewrite and body.get("model") in cfg.model_rewrite:
                body["model"] = cfg.model_rewrite[body["model"]]
                changed = True
            if changed:
                out_raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

        url = upstream_for(path)
        headers = fwd_headers(request, dialect.name if dialect else None)
        try:
            ureq = client.build_request(request.method, url, headers=headers, content=out_raw,
                                        params=request.query_params)
            resp = await client.send(ureq, stream=True)
        except httpx.HTTPError as exc:
            logger.error("upstream error: %s", exc)
            return JSONResponse({"error": {"message": f"optlimit upstream error: {exc}"}}, status_code=502)

        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _STRIP_RESP}
        if body is None or resp.status_code >= 400:
            data = await resp.aread()
            await resp.aclose()
            if body is not None:
                meta.update({"action": "passthrough_error", "status": resp.status_code,
                             "error_preview": data[:300].decode("utf-8", "replace"),
                             "latency_ms": round((time.monotonic() - t0) * 1000)})
                rec.write(meta)
            return Response(content=data, status_code=resp.status_code, headers=resp_headers)

        state = {"usage": None, "decision": None, "signal": None}

        def on_usage(u: dict) -> int | None:
            first = state["usage"] is None or "prompt" not in state["usage"]
            state["usage"] = D.merge_usage(state["usage"], u)
            if first and u.get("prompt") is not None:
                d = ctl.on_prompt(key, kind, float(u["prompt"]), float(u.get("cached") or 0),
                                  window_start_hint=ws_hint)
                state["decision"] = d
                state["signal"] = signal_for(d, int(u["prompt"]), key if kind == "main" else None)
                if d.compact_now:
                    logger.info("compact_now %s session=%s turn=%d P=%d L*=%.0f g=%.0f (%s)",
                                req_id, key[:12], d.turn_idx, u["prompt"], d.limit or 0, d.g_hat or 0, cfg.trigger)
            return state["signal"]

        def finish(status: int) -> None:
            u = state["usage"] or {}
            d = state["decision"]
            if u.get("api_compaction_input"):
                # the API compacted inside this request: its summarization pass is the compaction cost
                ctl.note_summary_usage(key, float(u["api_compaction_input"]),
                                       completion=float(u.get("api_compaction_output") or 0))
                meta.update({"api_compaction_input": u["api_compaction_input"],
                             "api_compaction_output": u.get("api_compaction_output")})
            if kind == "summary" and u.get("prompt") is not None:
                # the harness's own compaction call: feeds the learned Lp / O_s / gamma terms (C)
                ctl.note_summary_usage(key, float(u["prompt"]), cached=float(u.get("cached") or 0),
                                       completion=float(u.get("output") or 0))
            meta.update({
                "action": "forwarded", "status": status,
                "prompt": u.get("prompt"), "cached": u.get("cached"), "cache_write": u.get("cache_write"),
                "uncached": u.get("uncached"), "output": u.get("output"), "reasoning": u.get("reasoning"),
                "decision": d.as_dict() if d else None, "reported_prompt": state["signal"],
                "latency_ms": round((time.monotonic() - t0) * 1000),
            })
            rec.write(meta)

        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            async def stream():
                buf = b""
                try:
                    async for chunk in resp.aiter_bytes():   # decoded, in case the upstream compresses anyway
                        buf += chunk
                        while True:
                            i = buf.find(b"\n\n")
                            j = buf.find(b"\r\n\r\n")
                            if i < 0 and j < 0:
                                break
                            if j >= 0 and (i < 0 or j < i):
                                ev, buf = buf[:j], buf[j + 4:]
                                sep = b"\r\n\r\n"
                            else:
                                ev, buf = buf[:i], buf[i + 2:]
                                sep = b"\n\n"
                            yield D.rewrite_sse_event(ev, dialect, on_usage, state["signal"]) + sep
                    if buf:
                        yield D.rewrite_sse_event(buf, dialect, on_usage, state["signal"])
                finally:
                    finish(resp.status_code)
            return StreamingResponse(stream(), status_code=resp.status_code, headers=resp_headers,
                                     background=BackgroundTask(resp.aclose))

        data = await resp.aread()
        await resp.aclose()
        try:
            obj = json.loads(data)
            u = dialect.usage_from_json(obj) if isinstance(obj, dict) else None
            if u is not None:
                sig = on_usage(u)
                if sig is not None:
                    dialect.rewrite_json(obj, sig)
                    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            logger.exception("response parse failed; relaying verbatim")
        finish(resp.status_code)
        return Response(content=data, status_code=resp.status_code, headers=resp_headers)

    async def status(_: Request) -> JSONResponse:
        c = cfg.controller
        return JSONResponse({"name": "optlimit", "trigger": cfg.trigger, "mode": c.mode,
                             "fixed_limit": c.fixed_limit, "Lp": c.Lp, "alpha": c.alpha, "beta": c.beta,
                             "m": c.m, "gamma": c.gamma, "g_prior": c.g_prior, "sessions": len(ctl.sessions),
                             "requests": rec.n, "log": cfg.log_path})

    async def sessions(_: Request) -> JSONResponse:
        return JSONResponse([s.summary() for s in ctl.sessions.values()])

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await client.aclose()

    app = Starlette(routes=[
        Route("/__optlimit__/status", status),
        Route("/__optlimit__/sessions", sessions),
        Route("/{path:path}", handle, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
    ], lifespan=lifespan)
    app.state.controller = ctl
    app.state.recorder = rec
    app.state.config = cfg
    return app
