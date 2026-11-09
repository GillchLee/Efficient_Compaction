"""Harness profiles: everything optlimit needs to know about *how* a harness compacts, in one place.

The policy (optlimit.controller) and the benchmark (configs/benchmarks.yaml) know nothing about
harnesses; the proxy (dialects), the Harbor adapters and the config generator read these profiles.
Adding a harness = one profile here (+ a Harbor subclass in optlimit/adapters/ if needed).

    from optlimit.harnesses import get
    p = get("codex")
    p.trigger, p.band, p.native_kwargs(40000)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HarnessProfile:
    name: str
    agent: dict                       # Harbor agent entry: {"import_path": ...} or {"name": ...}
    connection: str                   # "in-process" (adapter calls the summarizer) | "proxy" (CLI behind optlimit.proxy)
    # proxy only -------------------------------------------------------------------------------
    trigger: str | None = None        # how the proxy makes the harness compact: "usage" | "error"
    band: dict = field(default_factory=dict)   # low_cap / high / limit_max (tokens)
    native_band: dict = field(default_factory=dict)  # harness threshold inside the band (proxy mode)
    agent_kwargs: dict = field(default_factory=dict)  # fixed agent kwargs (harnesses without a `compaction:` kwarg)
    min_window_turns: int = 3         # proxy-side guard: turns a window lasts before the next compaction
    # request classification (proxy) -------------------------------------------------------------
    summary_prefixes: tuple[str, ...] = ()     # last user message starts with: the harness's compaction call
    summary_markers: tuple[str, ...] = ()      # ... or contains (appended after a tool result)
    window_start_prefixes: tuple[str, ...] = ()  # first user message of a new window
    default_prior: str | None = None  # configs/priors.json row
    notes: str = ""

    def native_kwargs(self, limit: int | None) -> dict:
        """Agent kwargs that put the harness's *own* threshold at `limit` (static mode)."""
        if limit is None:
            return {}
        if self.name == "terminus-2":
            return {}                 # terminus-2 static limits go through optlimit mode fixed (exact)
        if self.name == "claude-code":
            return {"compaction": {"limit": int(limit)}}
        if self.name == "codex":
            return {"compaction": {"limit": int(limit)}}
        if self.name == "terminus-2-proxy":
            raise ValueError("a static limit for terminus-2 needs the in-process adapter (--harness terminus-2 on "
                             "the lhtb-fork profile); behind the proxy use --proxy-fixed N")
        raise KeyError(self.name)

    def proxy_kwargs(self) -> dict:
        """Agent kwargs in proxy mode: the harness's own threshold parked high inside the band, so
        only the proxy decides."""
        if self.agent_kwargs:
            return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.agent_kwargs.items()}
        return {"compaction": dict(self.native_band)}


PROFILES: dict[str, HarnessProfile] = {
    "terminus-2": HarnessProfile(
        name="terminus-2",
        agent={"import_path": "optlimit.harbor_agent:OptlimitTerminus2"},
        connection="in-process",
        summary_prefixes=(
            "You are about to hand off your work to another AI agent",
            "You are picking up work from a previous AI agent on this task",
            "The next agent has a few questions for you",
        ),
        window_start_prefixes=("Here are the answers the other agent provided.",),
        default_prior="terminus2-gpt-6-luna",
        notes="summary + questions + answers (m ~ 2.2); the adapter calls _summarize() directly",
    ),
    # Stock terminus-2 (any Harbor, no patches) with its LLM calls routed through the proxy: a
    # "prompt is too long" 400 raises ContextLengthExceededError and terminus-2 runs its own
    # three-call hand-off (enable_summarize). Used where the in-process adapter is unavailable
    # (Harbor 0.25 profile). The questions call starts from an empty history, so the proxy sees it
    # as its own session: its cost is in the log but not in the main session's learned C.
    "terminus-2-proxy": HarnessProfile(
        name="terminus-2-proxy",
        agent={"name": "terminus-2"},
        connection="proxy",
        trigger="error",
        band={"low_cap": 50_000, "high": 950_000, "limit_max": 180_000},
        native_band={"window": 200_000},      # terminus-2's own trigger stays at the model window
        agent_kwargs={"parser_name": "json", "enable_summarize": True, "proactive_summarization_threshold": 8000,
                      "record_terminal_session": True, "llm_call_kwargs": {"max_tokens": 16384, "num_retries": 6}},
        min_window_turns=3,
        summary_prefixes=(
            "You are about to hand off your work to another AI agent",
            "You are picking up work from a previous AI agent on this task",
            "The next agent has a few questions for you",
        ),
        summary_markers=("You are about to hand off your work to another AI agent",),
        window_start_prefixes=("Here are the answers the other agent provided.",),
        default_prior="terminus2-claude-haiku-5-5",
        notes="stock terminus-2 behind the proxy (trigger=error); summary + questions + answers (m ~ 2.2)",
    ),
    "claude-code": HarnessProfile(
        name="claude-code",
        agent={"import_path": "optlimit.adapters.harbor_installed:OptlimitClaudeCode"},
        connection="proxy",
        # Claude Code 2.1.3xx ignores the reported usage; a "prompt is too long" 400 makes it run its
        # reactive compaction. It aborts after 3 compactions within 3 turns of each other.
        trigger="error",
        band={"low_cap": 50_000, "high": 950_000, "limit_max": 180_000},
        native_band={"window": 200_000},
        min_window_turns=4,
        summary_prefixes=("Your task is to create a detailed summary of the conversation so far",),
        summary_markers=("Your task is to create a detailed summary of the conversation so far",),
        window_start_prefixes=("This session is being continued from a previous conversation",),
        default_prior="claude-code-hub",
        notes="one summary request (m=1); its own threshold fires ~25% before CLAUDE_CODE_AUTO_COMPACT_WINDOW x PCT",
    ),
    "codex": HarnessProfile(
        name="codex",
        agent={"import_path": "optlimit.adapters.harbor_installed:OptlimitCodex"},
        connection="proxy",
        # compacts when the last response's total_tokens >= model_auto_compact_token_limit (<= 90% window)
        trigger="usage",
        band={"low_cap": 50_000, "high": 258_000, "limit_max": 180_000},
        native_band={"limit": 200_000},
        min_window_turns=3,
        summary_prefixes=("You are performing a CONTEXT CHECKPOINT COMPACTION",),
        summary_markers=("You are performing a CONTEXT CHECKPOINT COMPACTION",),
        window_start_prefixes=("Another language model started to solve this problem",),
        default_prior="codex-luna-hub",
        notes="local compaction behind a custom provider: one summary request without tools (m=1)",
    ),
}

# compaction requests of other tools seen through the proxy (no profile of their own)
EXTRA_SUMMARY_PREFIXES = ("Summarize the conversation so far",)


def get(name: str) -> HarnessProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown harness {name!r}; known: {', '.join(PROFILES)}") from None


def all_summary_prefixes() -> tuple[str, ...]:
    return tuple(dict.fromkeys(p for h in PROFILES.values() for p in h.summary_prefixes)) + EXTRA_SUMMARY_PREFIXES


def all_summary_markers() -> tuple[str, ...]:
    return tuple(dict.fromkeys(m for h in PROFILES.values() for m in h.summary_markers))


def all_window_start_prefixes() -> tuple[str, ...]:
    return tuple(dict.fromkeys(p for h in PROFILES.values() for p in h.window_start_prefixes))
