"""Per-session threshold controller: observes prompt sizes, estimates g, and decides
when the harness should compact.

The decision is taken on the *response* of turn t (prompt P_t known from usage):

    compact_now  <=>  P_t + lookahead * g_hat  >=  L*(L0, Lp, g_hat, prices)

i.e. "the prompt about to be sent would exceed the threshold" (PDF definition 2),
predicting the next prompt with the current growth estimate.  optlimit.harbor_agent
turns compact_now into terminus-2's own summarization, so the harness runs its
*native* compaction and only the moment changes.

With g_prior_weight = 0 the estimator starts from nothing: no L* is computed until
the first growth observation (limit() returns None, reason "no_g"), after that
g_hat is the plain (clipped) mean of the observed increments of the session.

Adaptivity.  L* is a function of the price ratios alpha = write/read and beta =
output/read as much as of g: L* = L0 + Lp - g + sqrt(2 g C) with
C = (alpha-1)(Lp-g) + m gamma (L0+Lp-g) + beta O_s.  Two things make the policy
follow the ratios instead of freezing them at start-up:

* `update_prices(alpha, beta)` replaces the ratios for every later decision (the
  harbor agent calls it when configs/prices.json changes on disk, `prices_reload`);
  gamma is re-derived from the observed uncached fraction when `learn_gamma` is on,
  so a new alpha propagates into the summary-read term as well.
* `learn_lp` / `learn_summary_out` / `learn_gamma` replace the prior Lp, O_s and
  gamma by shrunken means of what the session's own compactions showed
  (prior weight `learn_weight` compactions), so C - and with it L* - tracks the
  actual hand-off size and cache behaviour of this model/harness.

Every Decision carries the alpha/beta/gamma/Lp/summary_out it was computed with.

Balance mode (`mode: balance`) is the Dynamic-TCP-Acknowledgment rule applied to
compaction: every turn of a window re-reads the tokens accumulated since the window
started (its "rent", P_t - P_start, in cache-read units); compacting costs C once.
Compact when the accumulated rent of the window reaches C.  With constant growth this
fires at exactly the closed-form L*; with bursty growth it reacts to the tokens actually
re-read instead of to a mean, needs no g estimate (lookahead 0), and is 2-competitive
against the offline optimum for any increment sequence (Dooly-Goldman-Scott 2001).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, asdict, replace

from .cost_model import Params, best_threshold, f, min_L, order_cost, should_compact_now
from .estimator import GEstimator


@dataclass
class ControllerConfig:
    mode: str = "formula"          # formula | balance | fixed | off
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
    boundary_ratio: float = 0.6    # P_t < ratio * P_{t-1} => compaction happened (fallback detection)
    # online learning of the compaction-cost terms (prior = the fields above)
    learn_lp: bool = False         # Lp from prompt - cached at each window start
    learn_summary_out: bool = False  # summary_out from the summarization calls' completion tokens
    learn_gamma: bool = False      # gamma = 1 + (alpha - 1) * uncached fraction of the summary calls' input
    learn_weight: float = 2.0      # prior weight in compactions
    # minimum window from information loss: a compaction costs `reacq_turns` extra turns of
    # re-acquisition (analysis/reacquisition.py); keep that share of turns below reacq_eps,
    # i.e. K >= reacq_turns / reacq_eps, and charge those turns in the per-turn cost
    reacq_turns: float = 0.0
    reacq_eps: float = 0.1


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
    alpha: float | None = None
    beta: float | None = None
    gamma: float | None = None
    Lp: float | None = None
    summary_out: float | None = None
    rent: float | None = None      # balance mode: re-read tokens accumulated in this window
    C: float | None = None         # balance mode: cost of one compaction (read units)

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
    expect_boundary: bool = False  # a summary call happened: next main turn starts a window
    g: GEstimator | None = None
    last_decision: Decision | None = None
    prompts: list[float] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    limits: list[float | None] = field(default_factory=list)
    # observations for the learned terms
    lp_obs: list[float] = field(default_factory=list)
    so_obs: list[float] = field(default_factory=list)
    unc_obs: list[float] = field(default_factory=list)   # uncached fraction of summary-call input
    window_start_prompt: float | None = None
    window_turns: int = 0
    rent: float = 0.0             # balance mode: sum over the window of (P_t - P_start)
    _summ_prompt: float = 0.0     # accumulated over the calls of the compaction in progress
    _summ_cached: float = 0.0
    _summ_out: float = 0.0

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
        self.price_updates: list[dict] = []

    # ------------------------------------------------------------ adaptivity
    def update_prices(self, alpha: float, beta: float | None = None, note: str = "") -> None:
        """New price ratios for every later decision (prices changed, cache TTL changed, ...)."""
        self.cfg.alpha = float(alpha)
        if beta is not None:
            self.cfg.beta = float(beta)
        self.price_updates.append({"ts": time.time(), "alpha": self.cfg.alpha, "beta": self.cfg.beta, "note": note})

    def note_summary_usage(self, key: str, prompt: float, cached: float = 0.0, completion: float = 0.0) -> None:
        """Usage of one summarization call; folded into the learned terms at the next window start."""
        s = self.session(key)
        s._summ_prompt += float(prompt)
        s._summ_cached += float(cached)
        s._summ_out += float(completion)

    def _flush_compaction_obs(self, s: SessionState, prompt: float, cached: float) -> None:
        if prompt - cached > 0:
            s.lp_obs.append(prompt - cached)
        if s._summ_out > 0:
            s.so_obs.append(s._summ_out)
        if s._summ_prompt > 0:
            s.unc_obs.append(max(0.0, 1.0 - s._summ_cached / s._summ_prompt))
        s._summ_prompt = s._summ_cached = s._summ_out = 0.0

    def _shrunk(self, prior: float, obs: list[float]) -> float:
        w = self.cfg.learn_weight
        return (w * prior + sum(obs)) / (w + len(obs)) if obs else prior

    def Lp(self, s: SessionState) -> float:
        return self._shrunk(self.cfg.Lp, s.lp_obs) if self.cfg.learn_lp else self.cfg.Lp

    def summary_out(self, s: SessionState) -> float | None:
        c = self.cfg
        if c.learn_summary_out and s.so_obs:
            return self._shrunk(c.summary_out if c.summary_out is not None else c.Lp, s.so_obs)
        return c.summary_out

    def gamma(self, s: SessionState) -> float:
        c = self.cfg
        if c.learn_gamma and s.unc_obs:
            prior_unc = (c.gamma - 1) / (c.alpha - 1) if c.alpha > 1 else 0.0
            u = self._shrunk(prior_unc, s.unc_obs)
            return 1 + (c.alpha - 1) * u
        return c.gamma

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
        return Params(L0=L0, Lp=self.Lp(s), g=s.g.value, alpha=c.alpha, beta=c.beta, phi=c.phi,
                      m=c.m, gamma=self.gamma(s), summary_out=self.summary_out(s), summary_fixed=c.summary_fixed)

    def k_min(self) -> int:
        c = self.cfg
        return max(c.min_window_turns, math.ceil(c.reacq_turns / c.reacq_eps) if c.reacq_turns > 0 else 0)

    def compaction_cost(self, s: SessionState) -> float | None:
        """C: the one-off cost of a compaction in cache-read units (order_cost + re-acquired turns)."""
        p = self.params(s)
        if p is None:
            return None
        C = order_cost(p)
        if self.cfg.reacq_turns > 0:
            C += self.cfg.reacq_turns * f(best_threshold(p), p)
        return C

    def limit(self, s: SessionState) -> tuple[float | None, str]:
        c = self.cfg
        if c.mode == "off":
            return None, "off"
        if c.mode == "fixed":
            return c.fixed_limit, "fixed"
        if c.g_prior_weight <= 0 and s.g.n == 0:
            return None, "no_g"
        p = self.params(s)
        if p is None:
            return None, "no_L0"
        if c.reacq_turns > 0:
            # extra turns per compaction re-read the window at ~f(L): C grows by reacq * f(L)
            L_base = best_threshold(p)
            p = replace(p, summary_fixed=p.summary_fixed + c.reacq_turns * f(L_base, p))
        L = best_threshold(p)
        k_min = self.k_min()
        floor = p.L0 + self.Lp(s) + p.g * (k_min - 1)
        reason = "formula" if c.mode != "balance" else "balance_ref"
        if L < floor:
            L, reason = floor, "floor_min_window"
        if c.limit_min is not None and L < c.limit_min:
            L, reason = c.limit_min, "limit_min"
        if c.limit_max is not None and L > c.limit_max:
            L, reason = c.limit_max, "limit_max"
        return L, reason

    def note_kind(self, key: str, kind: str) -> None:
        s = self.session(key)
        if kind == "summary":
            s.expect_boundary = True
            s.pending_compact = False

    # ------------------------------------------------------------ window boundaries
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
        s.turn_idx += 1
        window_start = False
        if s.first_prompt is None:
            s.first_prompt = prompt
            s.window_start_prompt = prompt
            s.window_turns = 1
            s.rent = 0.0
        elif s.expect_boundary or window_start_hint or (
            s.last_main_prompt is not None and prompt < c.boundary_ratio * s.last_main_prompt
            and s.last_main_prompt > (s.first_prompt or 0) + 2 * s.g.value
        ):
            window_start = True
            s.window_idx += 1
            s.n_compactions += 1
            s.cached_after_compaction = cached or s.cached_after_compaction
            s.pending_compact = False
            self._flush_compaction_obs(s, prompt, cached)
            s.window_start_prompt = prompt
            s.window_turns = 1
            s.rent = 0.0
        elif s.last_main_prompt is not None:
            s.g.add(prompt - s.last_main_prompt)
            s.window_turns += 1
            s.rent += max(prompt - (s.window_start_prompt or prompt), 0.0)
        s.expect_boundary = False
        s.last_main_prompt = prompt

        limit, reason = self.limit(s)
        g_hat = s.g.value
        compact = False
        rent = C = None
        if c.mode == "balance" and not s.pending_compact and limit is not None:
            C = self.compaction_cost(s)
            rent = s.rent
            if C is not None:
                # next turn would add (P_t + g_hat - P_start) of rent: compact before paying it
                excess_next = max(prompt + c.lookahead * g_hat - (s.window_start_prompt or prompt), 0.0)
                L0 = self.L0(s) or 0.0
                if (rent + excess_next >= C and s.window_turns >= self.k_min()
                        and prompt > L0 + self.Lp(s)):
                    compact, reason = True, "balance"
        elif limit is not None and not s.pending_compact:
            predicted = prompt + c.lookahead * g_hat
            L0 = self.L0(s) or 0.0
            if predicted >= limit and prompt > L0 + self.Lp(s):
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
                     turn_idx=s.turn_idx, window_start=window_start, reason=reason,
                     alpha=c.alpha, beta=c.beta, gamma=self.gamma(s), Lp=self.Lp(s), summary_out=self.summary_out(s),
                     rent=rent, C=C)
        s.last_decision = d
        s.prompts.append(prompt); s.kinds.append(kind); s.limits.append(limit)
        return d

    def is_pending(self, key: str) -> bool:
        return self.session(key).pending_compact


# ---------------------------------------------------------------- config from a job file
def load_prices(name: str, config_dir=None):
    """Prices row of configs/prices.json (alpha = write/read, beta = output/read)."""
    import json
    from pathlib import Path

    from .cost_model import Prices

    config_dir = Path(config_dir) if config_dir else Path(__file__).resolve().parent.parent / "configs"
    with open(config_dir / "prices.json") as f:
        e = json.load(f)["models"][name]
    return Prices(read=e["read"], write=e["write"], output=e["output"], uncached=e.get("uncached"))


def config_from_spec(spec: dict | None, config_dir=None) -> ControllerConfig:
    """ControllerConfig from the `optlimit:` block of a job config (or any dict).

    `prior` (key of configs/priors.json "harness") fills Lp, L0 (as L0_mode fixed), g_prior,
    m, gamma, phi and summary_out; `prices` (key of configs/prices.json "models") fills
    alpha and beta. Explicit fields override both. Unknown keys raise, so a typo in a yaml
    does not pass silently. `prices_reload` is accepted and ignored here (the harbor agent
    re-reads the prices row when the file changes and calls update_prices()).
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
        pr = load_prices(prices, config_dir)
        base.update(alpha=pr.alpha, beta=pr.beta)
    known = {f.name for f in fields(ControllerConfig)}
    spec.pop("prices_reload", None)   # handled by the harbor agent (hot reload of the prices row)
    unknown = set(spec) - known
    if unknown:
        raise ValueError(f"optlimit: unknown controller fields {sorted(unknown)}; known: {sorted(known)}")
    base.update(spec)
    return ControllerConfig(**base)
