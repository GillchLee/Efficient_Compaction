"""Wire-format details for the three API dialects the harnesses speak.

anthropic : POST .../v1/messages            (Claude Code)
openai    : POST .../chat/completions       (Terminus-2 via litellm, Codex with wire_api="chat", OpenHands)
responses : POST .../responses              (Codex default)

Each dialect knows how to: key a session, read the latest user text (to classify
summary calls), count assistant turns, read usage from JSON / SSE events, and
rewrite usage (the harness-native trigger), and how a context-length error looks.
"""
from __future__ import annotations

import hashlib
import json
import re

from .harnesses import all_summary_markers, all_summary_prefixes, all_window_start_prefixes

# Request classification comes from the harness profiles (optlimit/harnesses.py): a last user
# message that starts with a summary prefix (or contains a summary marker, e.g. Claude Code 2.1.3xx
# appends its compaction prompt after a tool result) is that harness's own compaction call; a
# window-start prefix marks the first request of a new window.
SUMMARY_PREFIXES = all_summary_prefixes()
WINDOW_START_PREFIXES = all_window_start_prefixes()
SUMMARY_CONTAINS = (
    "compaction",
)
SUMMARY_MARKERS = all_summary_markers()


def detect(path: str) -> str | None:
    p = path.rstrip("/")
    if p.endswith("/messages"):
        return "anthropic"
    if p.endswith("/chat/completions"):
        return "openai"
    if p.endswith("/responses") or p.endswith("/responses/compact"):
        return "responses"
    return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t in ("text", "input_text", "output_text"):
                parts.append(b.get("text", ""))
            elif t == "tool_result":
                parts.append(_text(b.get("content")))
        return "\n".join(parts)
    return ""


# Characters of the first user message hashed into a "root:" session key. Large enough to reach
# past any harness prompt template (terminus-2: task text starts at ~2.9-3.4 KB) and cover the
# task description, small enough that a long initial terminal screen does not matter.
ROOT_HASH_CHARS = 32000


def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:16]


def _head(msgs: list) -> str:
    """Session root: the first system/developer message (if any) + the first user message."""
    head = ""
    seen_sys = seen_user = False
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or ""
        if role in ("system", "developer") and not seen_sys:
            head += role + ":" + _text(m.get("content"))[:4000] + "\n"
            seen_sys = True
        elif role == "user" and not seen_user:
            head += "user:" + _text(m.get("content"))[:ROOT_HASH_CHARS] + "\n"
            seen_user = True
        if seen_user:
            break
    return head


def _keystrokes(txt: str) -> list[str]:
    """Shell commands of a terminus-2 style JSON reply ({"commands": [{"keystrokes": "..."}]})."""
    try:
        i = txt.index("{")
        obj = json.loads(txt[i:txt.rindex("}") + 1])
        cmds = obj.get("commands") if isinstance(obj, dict) else None
        if isinstance(cmds, list):
            return [str(c.get("keystrokes") or "") for c in cmds if isinstance(c, dict) and c.get("keystrokes")]
    except (ValueError, AttributeError):
        pass
    return [m.encode().decode("unicode_escape", "replace") for m in re.findall(r'"keystrokes"\s*:\s*"((?:[^"\\]|\\.)*)"', txt)]


class Dialect:
    name = ""
    messages_key = "messages"

    # ---- request inspection
    def messages(self, body: dict) -> list:
        v = body.get(self.messages_key)
        return v if isinstance(v, list) else []

    def session_key(self, body: dict, headers: dict | None = None) -> str:
        raise NotImplementedError

    def last_user_is_last_item(self, body: dict) -> bool:
        """True when the newest user message is the final item (first request of a window)."""
        msgs = self.messages(body)
        return bool(msgs) and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "user"

    def first_user_text(self, body: dict) -> str:
        """Text of the FIRST content block of the first user message.

        Only the first block: litellm merges consecutive user messages into one message with
        several text blocks, and terminus-2 rebuilds its chat after summarization as
        [original prompt, question prompt, questions, hand-off], so the first user message
        then carries [original, question] blocks. Hashing the whole message gave every
        window a new session; the first block alone is the original prompt in every window.
        """
        for m in self.messages(body):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            return str(b.get("text") or "")
                        if isinstance(b, str):
                            return b
                    return ""
                return _text(c)
        return ""

    def root_preview(self, body: dict) -> str:
        return self.first_user_text(body)[:300]

    def last_user_text(self, body: dict) -> str:
        for m in reversed(self.messages(body)):
            if isinstance(m, dict) and m.get("role") == "user":
                return _text(m.get("content"))
        return ""

    # ---- tool calls visible in the request (optlimit/reread.py)
    def tool_events(self, body: dict) -> list[dict]:
        """[{id, name, text, output}] for every tool call in the request, in order.

        Chat Completions: assistant `tool_calls` + `tool` messages; terminus-2 style agents put
        {"commands": [{"keystrokes": ...}]} in the assistant text and get the terminal screen back
        as the next user message.
        """
        out: list[dict] = []
        by_id: dict[str, dict] = {}
        msgs = self.messages(body)
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    ev = {"id": tc.get("id"), "name": str(fn.get("name") or ""), "text": str(fn.get("arguments") or ""),
                          "output": None}
                    out.append(ev)
                    if ev["id"]:
                        by_id[ev["id"]] = ev
                txt = _text(m.get("content"))
                if '"keystrokes"' in txt:
                    nxt = msgs[i + 1] if i + 1 < len(msgs) and isinstance(msgs[i + 1], dict) else None
                    screen = _text(nxt.get("content")) if nxt and nxt.get("role") == "user" else None
                    for n, ks in enumerate(_keystrokes(txt)):
                        out.append({"id": "ks:" + _h(f"{i}:{n}:{ks}"), "name": "keystrokes", "text": ks,
                                    "output": screen})
            elif role == "tool":
                ev = by_id.get(m.get("tool_call_id"))
                if ev is not None:
                    ev["output"] = _text(m.get("content"))
        return out

    def inject_carry(self, body: dict, text: str) -> bool:
        """Put the carry-over block behind the compaction summary; False when not supported."""
        return False

    def n_assistant(self, body: dict) -> int:
        return sum(1 for m in self.messages(body) if isinstance(m, dict) and m.get("role") == "assistant")

    def classify(self, body: dict) -> tuple[str, bool]:
        """-> (kind, window_start_hint). kind: main | summary."""
        t = self.last_user_text(body).lstrip()
        for p in SUMMARY_PREFIXES:
            if t.startswith(p):
                return "summary", False
        if any(m in t for m in SUMMARY_MARKERS):
            return "summary", False
        for p in WINDOW_START_PREFIXES:
            if t.startswith(p):
                # the handoff message stays the newest user message for the whole window
                # (later turns only append tool items), so flag only its first request
                return "main", self.last_user_is_last_item(body)
        return "main", False

    def is_stream(self, body: dict) -> bool:
        return bool(body.get("stream"))

    def prepare_request(self, body: dict) -> bool:
        """Mutate the outgoing body if needed (e.g. ask for usage in streams). -> changed?"""
        return False

    # ---- usage
    def usage_from_json(self, obj: dict) -> dict | None:
        raise NotImplementedError

    def usage_from_event(self, event: str | None, obj: dict) -> dict | None:
        """Usage carried by one SSE data object (may be partial: see merge)."""
        raise NotImplementedError

    def rewrite_json(self, obj: dict, prompt_signal: int) -> None:
        raise NotImplementedError

    def rewrite_event(self, event: str | None, obj: dict, prompt_signal: int) -> None:
        raise NotImplementedError

    def context_error(self, prompt_est: int, limit: int) -> tuple[int, dict]:
        raise NotImplementedError


def merge_usage(acc: dict | None, new: dict) -> dict:
    """Later events override earlier ones per field; prompt fields come first."""
    if acc is None:
        return dict(new)
    out = dict(acc)
    for k, v in new.items():
        if v is not None:
            out[k] = v
    return out


# ---------------------------------------------------------------- Anthropic

class Anthropic(Dialect):
    name = "anthropic"

    def tool_events(self, body: dict) -> list[dict]:
        out: list[dict] = []
        by_id: dict[str, dict] = {}
        for m in self.messages(body):
            c = m.get("content") if isinstance(m, dict) else None
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    ev = {"id": b.get("id"), "name": str(b.get("name") or ""),
                          "text": json.dumps(b.get("input"), ensure_ascii=False), "output": None}
                    out.append(ev)
                    if ev["id"]:
                        by_id[ev["id"]] = ev
                elif b.get("type") == "tool_result":
                    ev = by_id.get(b.get("tool_use_id"))
                    if ev is not None:
                        ev["output"] = _text(b.get("content"))
        return out

    def session_key(self, body: dict, headers: dict | None = None) -> str:
        meta = body.get("metadata")
        if isinstance(meta, dict):
            raw = meta.get("user_id")
            if isinstance(raw, str) and raw.startswith("{"):
                try:
                    sid = json.loads(raw).get("session_id")
                    if sid:
                        return "cc:" + str(sid)
                except Exception:
                    pass
        sys_ = body.get("system")
        sys_t = sys_ if isinstance(sys_, str) else _text(sys_)
        # Hash the whole FIRST TEXT BLOCK of the first user message, not a 300-char prefix:
        # Harbor's terminus-2 puts the task instruction ~3 KB into a fixed prompt template, so
        # the prefix made every concurrent terminus-2 trial share one controller session
        # (2026-11-11 SWE-Marathon balance run). The first block is the original prompt in
        # every window (see first_user_text), so the key is stable across compactions.
        return "root:" + _h(sys_t[:4000] + "\n" + self.first_user_text(body)[:ROOT_HASH_CHARS])

    def usage_from_json(self, obj: dict) -> dict | None:
        u = obj.get("usage")
        if not isinstance(u, dict):
            return None
        return self._norm(u)

    @staticmethod
    def _norm(u: dict) -> dict:
        inp = u.get("input_tokens") or 0
        cw = u.get("cache_creation_input_tokens") or 0
        cr = u.get("cache_read_input_tokens") or 0
        out = {"prompt": inp + cw + cr, "cached": cr, "cache_write": cw, "uncached": inp,
               "output": u.get("output_tokens") or 0, "reasoning": 0}
        out.update(Anthropic._api_compaction(u))
        return out

    @staticmethod
    def _api_compaction(u: dict) -> dict:
        """Server-side compaction (compact_20260112 / on-demand): billed as usage.iterations entries
        of type "compaction", excluded from the top-level counts."""
        its = [i for i in (u.get("iterations") or []) if isinstance(i, dict) and i.get("type") == "compaction"]
        if not its:
            return {}
        return {"api_compaction_input": sum((i.get("input_tokens") or 0) for i in its),
                "api_compaction_output": sum((i.get("output_tokens") or 0) for i in its)}

    def usage_from_event(self, event, obj):
        t = obj.get("type")
        if t == "message_start":
            u = (obj.get("message") or {}).get("usage")
            return self._norm(u) if isinstance(u, dict) else None
        if t == "message_delta":
            u = obj.get("usage")
            if isinstance(u, dict):
                out = {"output": u.get("output_tokens"), **self._api_compaction(u)}
                if "input_tokens" in u and u.get("input_tokens") is not None:
                    n = self._norm(u)
                    out.update({"prompt": n["prompt"], "cached": n["cached"], "cache_write": n["cache_write"],
                                "uncached": n["uncached"]})
                return out
        return None

    @staticmethod
    def _rewrite(u: dict, prompt_signal: int) -> None:
        u["input_tokens"] = int(prompt_signal)
        for k in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if k in u:
                u[k] = 0
        if isinstance(u.get("cache_creation"), dict):
            for k in u["cache_creation"]:
                u["cache_creation"][k] = 0

    def rewrite_json(self, obj, prompt_signal):
        if isinstance(obj.get("usage"), dict):
            self._rewrite(obj["usage"], prompt_signal)

    def rewrite_event(self, event, obj, prompt_signal):
        t = obj.get("type")
        if t == "message_start" and isinstance((obj.get("message") or {}).get("usage"), dict):
            self._rewrite(obj["message"]["usage"], prompt_signal)
        elif t == "message_delta" and isinstance(obj.get("usage"), dict) and "input_tokens" in obj["usage"]:
            self._rewrite(obj["usage"], prompt_signal)

    def context_error(self, prompt_est, limit):
        return 400, {"type": "error", "error": {
            "type": "invalid_request_error",
            "message": f"prompt is too long: {prompt_est} tokens > {limit} maximum"}}


# ---------------------------------------------------------------- OpenAI chat completions

class OpenAIChat(Dialect):
    name = "openai"

    def session_key(self, body: dict, headers: dict | None = None) -> str:
        return "root:" + _h(_head(self.messages(body)))

    def prepare_request(self, body: dict) -> bool:
        if body.get("stream"):
            so = body.get("stream_options")
            if not isinstance(so, dict) or not so.get("include_usage"):
                body["stream_options"] = dict(so or {}, include_usage=True)
                return True
        return False

    @staticmethod
    def _norm(u: dict) -> dict:
        pd = u.get("prompt_tokens_details") or {}
        cd = u.get("completion_tokens_details") or {}
        p = u.get("prompt_tokens") or 0
        c = (pd.get("cached_tokens") if isinstance(pd, dict) else 0) or 0
        w = (pd.get("cache_write_tokens") if isinstance(pd, dict) else 0) or 0   # GPT-6 reports writes
        c = min(c, p); w = min(w, p - c)
        return {"prompt": p, "cached": c, "cache_write": w, "uncached": p - c - w,
                "output": u.get("completion_tokens") or 0,
                "reasoning": (cd.get("reasoning_tokens") if isinstance(cd, dict) else 0) or 0}

    def usage_from_json(self, obj):
        u = obj.get("usage")
        return self._norm(u) if isinstance(u, dict) else None

    def usage_from_event(self, event, obj):
        return self.usage_from_json(obj)

    @staticmethod
    def _rewrite(u: dict, prompt_signal: int) -> None:
        comp = u.get("completion_tokens") or 0
        u["prompt_tokens"] = int(prompt_signal)
        u["total_tokens"] = int(prompt_signal) + comp
        pd = u.get("prompt_tokens_details")
        if isinstance(pd, dict) and "cached_tokens" in pd:
            pd["cached_tokens"] = 0

    def rewrite_json(self, obj, prompt_signal):
        if isinstance(obj.get("usage"), dict):
            self._rewrite(obj["usage"], prompt_signal)

    def rewrite_event(self, event, obj, prompt_signal):
        self.rewrite_json(obj, prompt_signal)

    def context_error(self, prompt_est, limit):
        return 400, {"error": {
            "message": (f"This model's maximum context length is {limit} tokens. However, you requested "
                        f"{prompt_est} tokens in the messages. Please reduce the length of the messages."),
            "type": "invalid_request_error", "param": "messages", "code": "context_length_exceeded"}}


# ---------------------------------------------------------------- OpenAI responses

class OpenAIResponses(Dialect):
    name = "responses"
    messages_key = "input"

    def messages(self, body: dict) -> list:
        v = body.get("input")
        if isinstance(v, str):
            return [{"role": "user", "content": v}]
        return v if isinstance(v, list) else []

    def _is_msg(self, item) -> bool:
        return isinstance(item, dict) and (item.get("type") in (None, "message")) and "role" in item

    CALL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")
    OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output", "local_shell_call_output")
    CARRY_MARK = "[Carried over from before the context compaction]"

    def tool_events(self, body: dict) -> list[dict]:
        out: list[dict] = []
        by_id: dict[str, dict] = {}
        for it in self.messages(body):
            if not isinstance(it, dict):
                continue
            t = it.get("type")
            if t in self.CALL_TYPES:
                raw = it.get("input") if it.get("input") is not None else it.get("arguments")
                if raw is None and it.get("action") is not None:
                    raw = json.dumps(it.get("action"), ensure_ascii=False)
                ev = {"id": it.get("call_id") or it.get("id"), "name": str(it.get("name") or t),
                      "text": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False), "output": None}
                out.append(ev)
                if ev["id"]:
                    by_id[ev["id"]] = ev
            elif t in self.OUTPUT_TYPES:
                ev = by_id.get(it.get("call_id"))
                if ev is not None:
                    o = it.get("output")
                    ev["output"] = o if isinstance(o, str) else _text(o)
        return out

    def inject_carry(self, body: dict, text: str) -> bool:
        """Insert the carry-over block as a user message right behind the compaction summary (the last
        `compaction` item, or the last hand-off message "Another language model started ...").

        Codex does not know about the block, so the proxy re-inserts the identical item at the same
        place in every request of the window (keeps the prompt-cache prefix stable).
        """
        items = body.get("input")
        if not text or not isinstance(items, list):
            return False
        idx = None
        starts = tuple(all_window_start_prefixes())
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            if it.get("type") == "compaction":
                idx = i                      # remote compaction: encrypted summary item
            elif self._is_msg(it) and it.get("role") == "user" and starts \
                    and _text(it.get("content")).lstrip().startswith(starts):
                idx = i                      # local compaction: plain-text hand-off message
        if idx is None:
            return False
        nxt = items[idx + 1] if idx + 1 < len(items) else None
        if self._is_msg(nxt) and _text(nxt.get("content")).startswith(self.CARRY_MARK):
            return False
        items.insert(idx + 1, {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": text}]})
        return True

    THREAD_HEADERS = ("x-codex-thread-id", "session_id", "conversation_id", "x-client-request-id")

    def session_key(self, body: dict, headers: dict | None = None) -> str:
        # Codex tags every request of a thread with its thread id; prefer that, because the
        # message prefix changes after compaction (the task message is replaced by the handoff).
        if headers:
            for hk in self.THREAD_HEADERS:
                v = headers.get(hk)
                if v:
                    return "cx:" + str(v)[:64]
        # Fallback: Codex opens every thread with the same <environment_context> user message and
        # puts the task text in the next user message, so hash every leading user message (up to
        # the first assistant/function item) rather than just the first one.
        ins = body.get("instructions") or ""
        head = []
        for it in self.messages(body):
            if self._is_msg(it) and it.get("role") in ("user", "system", "developer"):
                head.append(it.get("role") + ":" + _text(it.get("content"))[:4000])
            else:
                break
        return "root:" + _h(ins[:4000] + "\n" + "\n".join(head))

    def root_preview(self, body: dict) -> str:
        # Codex: first user message is <environment_context>; the task text is the last of the
        # leading user messages, which is what harbor's task instruction matches.
        last = ""
        for it in self.messages(body):
            if self._is_msg(it) and it.get("role") in ("user", "system", "developer"):
                if it.get("role") == "user":
                    last = _text(it.get("content"))[:300]
            else:
                break
        return last

    def last_user_text(self, body: dict) -> str:
        for it in reversed(self.messages(body)):
            if self._is_msg(it) and it.get("role") == "user":
                return _text(it.get("content"))
        return ""

    def n_assistant(self, body: dict) -> int:
        return sum(1 for it in self.messages(body) if self._is_msg(it) and it.get("role") == "assistant")

    def classify(self, body: dict) -> tuple[str, bool]:
        # Codex >= 0.16x on the Responses wire compacts with a trailing {"type": "compaction_trigger"}
        # input item (no summary prompt; the task text stays the last user message) and starts the
        # next window with the returned {"type": "compaction", "encrypted_content": ...} item last.
        items = self.messages(body)
        last = items[-1] if items else None
        if isinstance(last, dict):
            if last.get("type") == "compaction_trigger":
                return "summary", False
            if last.get("type") == "compaction":
                return "main", True
        kind, hint = super().classify(body)
        if kind == "main":
            t = self.last_user_text(body).lower()[:400]
            if any(s in t for s in SUMMARY_CONTAINS) and "summary" in t:
                return "summary", False
        return kind, hint

    @staticmethod
    def _norm(u: dict) -> dict:
        idt = u.get("input_tokens_details") or {}
        odt = u.get("output_tokens_details") or {}
        p = u.get("input_tokens") or 0
        c = (idt.get("cached_tokens") if isinstance(idt, dict) else 0) or 0
        w = (idt.get("cache_write_tokens") if isinstance(idt, dict) else 0) or 0
        c = min(c, p); w = min(w, p - c)
        return {"prompt": p, "cached": c, "cache_write": w, "uncached": p - c - w,
                "output": u.get("output_tokens") or 0,
                "reasoning": (odt.get("reasoning_tokens") if isinstance(odt, dict) else 0) or 0}

    def usage_from_json(self, obj):
        u = obj.get("usage")
        if isinstance(u, dict):
            return self._norm(u)
        r = obj.get("response")
        if isinstance(r, dict) and isinstance(r.get("usage"), dict):
            return self._norm(r["usage"])
        return None

    def usage_from_event(self, event, obj):
        if obj.get("type") in ("response.completed", "response.incomplete", "response.done"):
            return self.usage_from_json(obj)
        return None

    @staticmethod
    def _rewrite(u: dict, prompt_signal: int) -> None:
        out = u.get("output_tokens") or 0
        u["input_tokens"] = int(prompt_signal)
        u["total_tokens"] = int(prompt_signal) + out
        d = u.get("input_tokens_details")
        if isinstance(d, dict) and "cached_tokens" in d:
            d["cached_tokens"] = 0

    def rewrite_json(self, obj, prompt_signal):
        if isinstance(obj.get("usage"), dict):
            self._rewrite(obj["usage"], prompt_signal)
        r = obj.get("response")
        if isinstance(r, dict) and isinstance(r.get("usage"), dict):
            self._rewrite(r["usage"], prompt_signal)

    def rewrite_event(self, event, obj, prompt_signal):
        if obj.get("type") in ("response.completed", "response.incomplete", "response.done"):
            self.rewrite_json(obj, prompt_signal)

    def context_error(self, prompt_est, limit):
        return 400, {"error": {
            "message": (f"Your input exceeds the context window of this model. This model's maximum context "
                        f"length is {limit} tokens; the request has {prompt_est} tokens."),
            "type": "invalid_request_error", "param": "input", "code": "context_length_exceeded"}}


DIALECTS: dict[str, Dialect] = {"anthropic": Anthropic(), "openai": OpenAIChat(), "responses": OpenAIResponses()}


def get(path: str) -> Dialect | None:
    n = detect(path)
    return DIALECTS.get(n) if n else None


# ---------------------------------------------------------------- SSE helpers

_EVENT_RE = re.compile(rb"^event:\s*(.+)$", re.M)


def split_event(raw: bytes) -> tuple[str | None, list[bytes]]:
    """-> (event name, lines). Lines keep their original bytes."""
    lines = raw.split(b"\n")
    ev = None
    m = _EVENT_RE.search(raw)
    if m:
        ev = m.group(1).strip().decode("utf-8", "replace")
    return ev, lines


def rewrite_sse_event(raw: bytes, dialect: Dialect, on_usage, prompt_signal: int | None) -> bytes:
    """Parse one SSE event (bytes without the trailing blank line). Report usage via
    on_usage(usage_dict) -> prompt_signal override (int) or None; rewrite data lines
    when a signal is given."""
    if b"data:" not in raw:
        return raw
    ev, lines = split_event(raw)
    out: list[bytes] = []
    for line in lines:
        if not line.startswith(b"data:"):
            out.append(line)
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]" or payload[:1] != b"{":
            out.append(line)
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            out.append(line)
            continue
        usage = dialect.usage_from_event(ev, obj) if isinstance(obj, dict) else None
        sig = prompt_signal
        if usage is not None:
            r = on_usage(usage)
            if r is not None:
                sig = r
        if usage is not None and sig is not None:
            dialect.rewrite_event(ev, obj, sig)
            out.append(b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        else:
            out.append(line)
    return b"\n".join(out)
