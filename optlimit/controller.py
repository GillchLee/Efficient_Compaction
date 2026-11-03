"""Per-session threshold controller: observes prompt sizes, estimates g, and decides
when the harness should compact.

The decision is taken on the *response* of turn t (prompt P_t known from usage):

    compact_now  <=>  P_t + lookahead * g_hat  >=  L*(L0, Lp, g_hat, prices)

i.e. "the prompt about to be sent would exceed the threshold" (PDF definition 2),
predicting the next prompt with the current growth estimate.  The proxy turns
compact_now into the harness's own trigger (usage rewrite or a context-length
error) so the harness runs its *native* compaction.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

from .cost_model import Params, best_threshold, min_L, should_compact_now
from .estimator import GEstimator


@dataclass
class ControllerConfig:
    mode: str = "formula"          # formula | fixed | off
    fixed_limit: float | None = None
    # cost-model parameters (prices in units of cache read)
    Lp: float = 8000.0
    alpha: float = 12.5
    beta: float = 0.0              # 0 -> ignore summary output (set from prices for model B)
    m: float = 0.0                 # full-context reads per compaction (0 -> model A)
    gamma: float = 1.0
    summary_out: float | None = None
    summary_fixed: float = 0.0
    phi: float = 0.0
    # g estimation
    g_prior: float = 1500.0
    g_prior_weight: float = 5.0
    g_mode: str = "mean"
    g_clip: float | None = 10.0
    # L0
    L0_mode: str = "first_prompt"  # first_prompt | fixed | cached_after_compaction
    L0_fixed: float | None = None
    # safety / shaping
    limit_min: float | None = None
    limit_max: float | None = None
    min_window_turns: int = 3      # L* >= L0 + Lp + g*(min_window_turns - 1)
    lookahead: float = 1.0
    horizon: int | None = None     # expected total turns; enables PDF 6(a) short-task rule
    boundary_ratio: float = 0.6    # P_t < ratio * P_{t-1} => compaction happened
    max_consecutive_rejects: int = 2


@dataclass
class Decision:
    compact_now: bool = False
    limit: float | None = None
    g_hat: float | None = None
    L0: float | None = None
    window_idx: int = 0
    turn_idx: int = 0
    window_start: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SessionState:
    key: str
    created: float = field(default_factory=time.time)
    first_prompt: float | None = None
    cached_after_compaction: float | None = None
    last_main_prompt: float | None = None
    turn_idx: int = 0              # main-loop turns seen
    window_idx: int = 0
    n_compactions: int = 0
    pending_compact: bool = False  # decided; waiting for the harness to act
    expect_boundary: bool = False  # a summary call / rejection happened: next main turn starts a window
    consecutive_rejects: int = 0
    g: GEstimator | None = None
    last_decision: Decision | None = None
    prompts: list[float] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    limits: list[float | None] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "key": self.key, "turns": self.turn_idx, "windows": self.window_idx,
            "compactions": self.n_compactions, "first_prompt": self.first_prompt,
            "last_main_prompt": self.last_main_prompt, "pending_compact": self.pending_compact,
            "g": self.g.snapshot() if self.g else None,
            "last_decision": self.last_decision.as_dict() if self.last_decision else None,
        }


class LimitController:
    def __init__(self, cfg: ControllerConfig):
        self.cfg = cfg
        self.sessions: dict[str, SessionState] = {}

    # ------------------------------------------------------------ helpers
    def session(self, key: str) -> SessionState:
        s = self.sessions.get(key)
        if s is None:
            s = SessionState(key=key, g=GEstimator(prior=self.cfg.g_prior, prior_weight=self.cfg.g_prior_weight,
                                                    mode=self.cfg.g_mode, clip_factor=self.cfg.g_clip))
            self.sessions[key] = s
        return s

    def L0(self, s: SessionState) -> float | None:
        c = self.cfg
        if c.L0_mode == "fixed":
            return c.L0_fixed
        if c.L0_mode == "cached_after_compaction" and s.cached_after_compaction:
            return s.cached_after_compaction
        return s.first_prompt if s.first_prompt is not None else c.L0_fixed

    def params(self, s: SessionState) -> Params | None:
        L0 = self.L0(s)
        if L0 is None:
            return None
        c = self.cfg
        return Params(L0=L0, Lp=c.Lp, g=s.g.value, alpha=c.alpha, beta=c.beta, phi=c.phi,
                      m=c.m, gamma=c.gamma, summary_out=c.summary_out, summary_fixed=c.summary_fixed)

    def limit(self, s: SessionState) -> tuple[float | None, str]:
        c = self.cfg
        if c.mode == "off":
            return None, "off"
        if c.mode == "fixed":
            return c.fixed_limit, "fixed"
        p = self.params(s)
        if p is None:
            return None, "no_L0"
        L = best_threshold(p)
        floor = p.L0 + p.Lp + p.g * (c.min_window_turns - 1)
        reason = "formula"
        if L < floor:
            L, reason = floor, "floor_min_window"
        if c.limit_min is not None and L < c.limit_min:
            L, reason = c.limit_min, "limit_min"
        if c.limit_max is not None and L > c.limit_max:
            L, reason = c.limit_max, "limit_max"
        return L, reason

    # ------------------------------------------------------------ request side (error mode)
    def should_reject(self, key: str, kind: str) -> bool:
        """Error-trigger mode: reject the next main-loop request after a compact_now decision."""
        s = self.session(key)
        if kind != "main" or not s.pending_compact:
            return False
        if s.consecutive_rejects >= self.cfg.max_consecutive_rejects:
            # the harness did not react to the error: fail open
            s.pending_compact = False
            s.consecutive_rejects = 0
            return False
        s.consecutive_rejects += 1
        s.expect_boundary = True
        return True

    def note_kind(self, key: str, kind: str) -> None:
        s = self.session(key)
        if kind == "summary":
            s.expect_boundary = True
            s.pending_compact = False

    # ------------------------------------------------------------ response side
    def on_prompt(self, key: str, kind: str, prompt: float, cached: float = 0.0,
                  window_start_hint: bool = False) -> Decision:
        """Called as soon as the prompt size of a response is known."""
        s = self.session(key)
        c = self.cfg
        if kind != "main":
            d = Decision(limit=s.last_decision.limit if s.last_decision else None,
                         g_hat=s.g.value, L0=self.L0(s), window_idx=s.window_idx, turn_idx=s.turn_idx,
                         reason=kind)
            return d
        s.consecutive_rejects = 0
        s.turn_idx += 1
        window_start = False
        if s.first_prompt is None:
            s.first_prompt = prompt
        elif s.expect_boundary or window_start_hint or (
            s.last_main_prompt is not None and prompt < c.boundary_ratio * s.last_main_prompt
            and s.last_main_prompt > (s.first_prompt or 0) + 2 * s.g.value
        ):
            window_start = True
            s.window_idx += 1
            s.n_compactions += 1
            s.cached_after_compaction = cached or s.cached_after_compaction
            s.pending_compact = False
        elif s.last_main_prompt is not None:
            s.g.add(prompt - s.last_main_prompt)
        s.expect_boundary = False
        s.last_main_prompt = prompt

        limit, reason = self.limit(s)
        g_hat = s.g.value
        compact = False
        if limit is not None and not s.pending_compact:
            predicted = prompt + c.lookahead * g_hat
            L0 = self.L0(s) or 0.0
            if predicted >= limit and prompt > L0 + c.Lp:
                compact = True
                if c.horizon is not None:
                    p = self.params(s)
                    R = max(c.horizon - s.turn_idx, 0)
                    if p is not None and not should_compact_now(predicted, R, p):
                        compact = False
                        reason += "+horizon_veto"
        if compact:
            s.pending_compact = True
        d = Decision(compact_now=compact, limit=limit, g_hat=g_hat, L0=self.L0(s), window_idx=s.window_idx,
                     turn_idx=s.turn_idx, window_start=window_start, reason=reason)
        s.last_decision = d
        s.prompts.append(prompt); s.kinds.append(kind); s.limits.append(limit)
        return d

    def is_pending(self, key: str) -> bool:
        return self.session(key).pending_compact


# ---------------------------------------------------------------- config from a job file
def config_from_spec(spec: dict | None, config_dir=None) -> ControllerConfig:
    """ControllerConfig from the `optlimit:` block of a job config (or any dict).

    `prior` (key of configs/priors.json "harness") fills Lp, L0 (as L0_mode fixed), g_prior,
    m, gamma, phi and summary_out; `prices` (key of configs/prices.json "models") fills
    alpha and beta. Explicit fields override both. Unknown keys raise, so a typo in a yaml
    does not pass silently.
    """
    import json
    from dataclasses import fields
    from pathlib import Path

    from .cost_model import Prices

    config_dir = Path(config_dir) if config_dir else Path(__file__).resolve().parent.parent / "configs"
    spec = dict(spec or {})
    base: dict = {}
    prior = spec.pop("prior", None)
    prices = spec.pop("prices", None)
    if prior:
        with open(config_dir / "priors.json") as f:
            p = json.load(f)["harness"][prior]
        base.update(Lp=float(p["Lp"]), g_prior=float(p["g"]), m=float(p.get("m") or 0),
                    gamma=float(p.get("gamma") or 1.0), phi=float(p.get("phi") or 0.0),
                    summary_out=p.get("summary_out"))
        if p.get("L0") is not None:
            base.update(L0_mode="fixed", L0_fixed=float(p["L0"]))
    if prices:
        with open(config_dir / "prices.json") as f:
            e = json.load(f)["models"][prices]
        pr = Prices(read=e["read"], write=e["write"], output=e["output"], uncached=e.get("uncached"))
        base.update(alpha=pr.alpha, beta=pr.beta)
    known = {f.name for f in fields(ControllerConfig)}
    unknown = set(spec) - known
    if unknown:
        raise ValueError(f"optlimit: unknown controller fields {sorted(unknown)}; known: {sorted(known)}")
    base.update(spec)
    return ControllerConfig(**base)
