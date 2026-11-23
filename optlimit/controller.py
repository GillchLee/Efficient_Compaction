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
  gamma by what the session's own compactions showed - by default the mean of the last
  `learn_recent` (3) compactions, the prior only until the first one; `learn_recent: 0`
  restores the shrunken mean over the whole session (prior weight `learn_weight`
  compactions) - so C - and with it L* - tracks the
  actual hand-off size and cache behaviour of this model/harness.

Every Decision carries the alpha/beta/gamma/Lp/summary_out it was computed with.

Balance mode (`mode: balance`) is the Dynamic-TCP-Acknowledgment rule applied to
compaction: every turn of a window re-reads the tokens accumulated since the window
started (its "rent", P_t - P_start, in cache-read units); compacting costs C once.
Compact when the accumulated rent of the window reaches C.  With constant growth this
fires at exactly the closed-form L*; with bursty growth it reacts to the tokens actually
re-read instead of to a mean, needs no g estimate (lookahead 0), and is 2-competitive
against the offline optimum for any increment sequence (Dooly-Goldman-Scott 2001).

Re-reads after a compaction (optlimit/reread.py detects them) feed three separate options:

* Re-read Charge (`reread_charge`, default on; old name `reread_cost`): the re-read calls of
  a window are charged to the compaction that caused them, C += R.  Changes *when* to compact.
* File Carry-over (`carry_files` > 0, default off; optlimit/carry.py): the latest read output
  of the files the agent keeps re-reading is attached behind the summary.  Changes *what* a
  compaction hands over; the controller only carries the budget.
* Whole-file Carry-over (`carry_whole` > 0, default off; optlimit/carry.py): a file re-read in
  `carry_whole_after` consecutive windows is handed over complete, read from disk at the compaction.
  The carried size enters Lp directly (note_carry).
* Parallel re-orientation (`reorient`, default off; optlimit/reorient.py): a note behind the summary asks
  the agent to fetch what it needs in one response.  With it re-reads take fewer calls but bring the same
  tokens; `reread_premium` (with `reread_productive`) adds (alpha - 1) x those tokens to C.
* Net rent (`rent_net_reread`, balance mode, default off): the prompt growth of re-read
  calls is left out of the rent.  Re-read Charge already counts those calls inside C, and
  re-reads cluster right after a compaction, so counting them in the rent as well made
  balance fire at 0.90-0.93 of L* on front-loaded windows (SWE-Marathon 2026-11-16).
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
    learn_weight: float = 2.0      # prior weight in compactions (used when learn_recent == 0)
    learn_recent: int = 3          # > 0: mean of the last N observed compactions, the prior is dropped after the
                                   # first one; 0: shrunken mean over the whole session (prior weight learn_weight)
    # re-reads after a compaction (optlimit/reread.py): the model calls a window spends reading again
    # files it had already seen before the compaction are charged to that compaction, C += R.
    # R = mean over the prior and the cost of those calls in the last `learn_recent` windows the
    # harness adapter / proxy reported (note_reread); the prior is reread_turns * (per-turn cost at
    # L* + beta * reread_turn_out) and counts as one window.  Measured 2026-11 (Codex + GPT-6 Luna, TB 2.1 / TB4 / SWE-Marathon):
    # 2.3-4.0 such calls per compaction regardless of benchmark, effort and limit.
    reread_charge: bool = True     # "Re-read Charge"
    reread_cost: bool | None = None  # deprecated alias of reread_charge (older configs / spec files)
    reread_turns: float = 3.5
    reread_turn_out: float = 300.0
    # "Productive-turn Charge" (default off): minimise the cost per productive turn, i.e. per turn that
    # is not a re-read.  A task needs a number of productive turns; a window of K turns delivers K - r of
    # them, so the total cost is proportional to W(K) / (K - r), not W(K) / K.  That optimum is the same
    # as the per-turn optimum with R = r * lambda, lambda = cost of one productive turn at the optimum
    # (solved by fixed-point iteration).  r = mean of the prior `reread_turns` (one window) and the
    # re-read calls of the last `learn_recent` windows; the observed cost of those calls is not used.
    # Needs reread_charge.  Prior codex-luna-medium: L* 47.8K -> 51.6K at r = 3.5, 59.3K at r = 5.
    reread_productive: bool = False
    # "Re-fetch premium" (default off, with reread_productive): the tokens a window fetches again after
    # the compaction (prompt growth of its re-read calls, W) are written at the input price a second
    # time; that premium (alpha - 1) * W is a cost of the compaction and is added to C before the
    # productive-turn fixed point.  W = mean over the last `learn_recent` windows (0 until the first
    # one).  Matters when re-reads are batched into one or two calls (optlimit/reorient.py): r falls
    # but W stays, and r alone would then understate the compaction.  docs/parallel_reorientation.md 3.1(a).
    reread_premium: bool = False
    # "Parallel re-orientation" (optlimit/reorient.py): note behind the compaction summary.
    # "parallel" = fetch everything needed in one response, "plain" = control wording, "off" = none.
    reorient: str = "off"
    # "File Carry-over": token budget of the block of earlier read outputs the proxy / terminus-2
    # adapter puts behind the summary for frequently re-read files (0 = off; optlimit/carry.py)
    carry_files: int = 0
    carry_item_tokens: int = 3000
    # which files go into the block first: "score" (re-read score) or "reread_edited" (files the last
    # window both re-read and edited first, then by score)
    carry_order: str = "score"
    # "Whole-file Carry-over": token budget for files handed over complete (0 = off).  A file re-read
    # in `carry_whole_after` consecutive windows is read from disk at the compaction and carried in full
    # (never cut; a file that does not fit stays with the snapshot block).  The carried size is known
    # before the window starts, so it is added to Lp at once instead of being learned over
    # `learn_recent` windows (note_carry).
    carry_whole: int = 0
    carry_whole_after: int = 2
    # minimum window from information loss: a compaction costs `reacq_turns` extra turns of
    # re-acquisition (analysis/reacquisition.py); keep that share of turns below reacq_eps,
    # i.e. K >= reacq_turns / reacq_eps, and charge those turns in the per-turn cost
    reacq_turns: float = 0.0
    reacq_eps: float = 0.1
    # formula mode: where the window starts in L* = P_s + g (K* - 1).  "model" = L0 + Lp (prior /
    # learned); "window_start" = the realized first prompt of the current window (same K*, no
    # estimate of P_s).  SWE-Marathon 2026-11-11: a 37K hand-off made the modeled L* fire after
    # 2K of growth (112% over that window's optimum); the realized base removes that case.
    formula_base: str = "model"
    # balance mode: compact when P_t + lookahead * g_hat >= hard_cap even if the rent has not
    # reached C (price tiers, context limits).  limit_max only caps the reference L*.
    hard_cap: float | None = None
    # balance mode: baseline the rent is measured from.  "window_start" = the window's first
    # prompt; "handoff" = max(first prompt, L0 + Lp), i.e. what a compaction would restart from
    # (differs only in the very first window, which starts near L0 without a hand-off).
    rent_base: str = "window_start"
    # balance mode, "Net rent": leave the prompt growth of re-read calls (the reply after a
    # call the re-read tracker flagged, note_reread_growth) out of the rent - the baseline
    # moves up by that growth.  Independent of reread_charge, meant to be used with it.
    rent_net_reread: bool = False

    def __post_init__(self) -> None:
        from .reorient import reorient_text
        reorient_text(self.reorient)              # unknown mode raises here, not at the first compaction
        if self.reread_cost is not None:          # old spelling wins only when given explicitly
            self.reread_charge = bool(self.reread_cost)
            self.reread_cost = None


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
    rr_growth: float | None = None # balance mode: prompt growth of re-read calls in this window
    C: float | None = None         # balance mode: cost of one compaction (read units)
    reread: float | None = None    # the re-read term R inside C (read units); None when off

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
    rr_turn_obs: list[float] = field(default_factory=list)  # re-read model calls per finished window
    rr_cost_obs: list[float] = field(default_factory=list)  # their cost (read units)
    rr_w_obs: list[float] = field(default_factory=list)     # prompt growth their output caused, per finished window
    window_start_prompt: float | None = None
    carry_now: float = 0.0        # tokens (estimate) of the whole files carried in the current window
    window_turns: int = 0
    rent: float = 0.0             # balance mode: sum over the window of (P_t - P_start)
    rr_growth: float = 0.0        # prompt growth of this window's re-read calls (Net rent)
    rr_growth_next: bool = False  # the next main prompt's increment comes from a re-read call
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

    def note_reread(self, key: str, turns: float, cost: float) -> None:
        """Re-read calls of a window that followed a compaction, and their cost in read units."""
        s = self.session(key)
        s.rr_turn_obs.append(float(turns))
        s.rr_cost_obs.append(float(cost))

    def note_carry(self, key: str, tokens: float) -> None:
        """Whole-file Carry-over: size of the files carried into the window that is starting (tokens).

        Call before the first prompt of the window is reported.  Lp = learned rest + this, so the
        limit follows the carried size from the first turn."""
        self.session(key).carry_now = max(float(tokens), 0.0)

    def note_reread_growth(self, key: str) -> None:
        """The tool calls whose output the next main prompt carries included a re-read (Net rent)."""
        self.session(key).rr_growth_next = True

    def call_cost(self, prompt: float, cached: float = 0.0, output: float = 0.0) -> float:
        """Cost of one model call in cache-read units."""
        c = self.cfg
        return float(cached) + c.alpha * max(float(prompt) - float(cached), 0.0) + c.beta * float(output)

    def reread_term(self, s: SessionState, p: Params) -> float:
        """Re-read Charge: R, what the re-reads after one compaction cost (read units)."""
        c = self.cfg
        if not c.reread_charge or c.mode in ("off", "fixed"):
            return 0.0
        if c.reread_productive:
            n = int(c.learn_recent or 0)
            tobs = s.rr_turn_obs[-n:] if n > 0 else s.rr_turn_obs
            r = max((c.reread_turns + sum(tobs)) / (1 + len(tobs)), 0.0)
            E = self.refetch_premium(s)
            return E + self.productive_charge(replace(p, summary_fixed=p.summary_fixed + E) if E > 0 else p, r)
        L = best_threshold(p)
        per_turn = (f(L, p) if L is not None and math.isfinite(f(L, p)) else p.L0 + p.Lp) + c.beta * c.reread_turn_out
        prior = c.reread_turns * per_turn
        obs = s.rr_cost_obs[-int(c.learn_recent):] if c.learn_recent and c.learn_recent > 0 else s.rr_cost_obs
        # re-reads per window vary a lot (0 .. 10+), so the prior keeps the weight of one window
        return max((prior + sum(obs)) / (1 + len(obs)), 0.0)

    def refetch_premium(self, s: SessionState) -> float:
        """Re-fetch premium: (alpha - 1) * W, W = tokens re-read calls brought back per window (read units)."""
        c = self.cfg
        if not c.reread_premium or not s.rr_w_obs:
            return 0.0
        n = int(c.learn_recent or 0)
        obs = s.rr_w_obs[-n:] if n > 0 else s.rr_w_obs
        return max(c.alpha - 1.0, 0.0) * sum(obs) / len(obs)

    def productive_charge(self, p: Params, r: float) -> float:
        """Productive-turn Charge: R = r * (cost per productive turn at the optimum), read units.

        Fixed point of R -> r * (f(L*(R)) + beta * out): with R inside C, f(L*) + beta * out is
        (W + R) / K = W / (K - r) when R = r * W / (K - r).  Converges at rate ~ r / K."""
        c = self.cfg
        if r <= 0:
            return 0.0
        R = 0.0
        for _ in range(60):
            q = replace(p, summary_fixed=p.summary_fixed + R)
            L = best_threshold(q)
            v = f(L, q) if L is not None else math.inf
            if not math.isfinite(v):
                v = p.L0 + p.Lp
            new = r * (v + c.beta * c.reread_turn_out)
            if abs(new - R) < 1.0:
                return new
            R = new
        return R

    def _flush_compaction_obs(self, s: SessionState, prompt: float, cached: float) -> None:
        if prompt - cached > 0:
            s.lp_obs.append(prompt - cached - s.carry_now)   # carry_now is added back in Lp()
        if s._summ_out > 0:
            s.so_obs.append(s._summ_out)
        if s._summ_prompt > 0:
            s.unc_obs.append(max(0.0, 1.0 - s._summ_cached / s._summ_prompt))
        s._summ_prompt = s._summ_cached = s._summ_out = 0.0

    def _shrunk(self, prior: float, obs: list[float]) -> float:
        if not obs:
            return prior
        n = int(self.cfg.learn_recent or 0)
        if n > 0:
            # Summary size barely drifts within a session (CV 0.2-0.3) but the prior can be 2-3x off,
            # and most sessions see only a few compactions: trust the session's own recent values at once.
            recent = obs[-n:]
            return sum(recent) / len(recent)
        w = self.cfg.learn_weight
        return (w * prior + sum(obs)) / (w + len(obs))

    def Lp(self, s: SessionState) -> float:
        lp = self._shrunk(self.cfg.Lp, s.lp_obs) if self.cfg.learn_lp else self.cfg.Lp
        return max(lp + s.carry_now, 0.0)

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
        p = Params(L0=L0, Lp=self.Lp(s), g=s.g.value, alpha=c.alpha, beta=c.beta, phi=c.phi,
                   m=c.m, gamma=self.gamma(s), summary_out=self.summary_out(s), summary_fixed=c.summary_fixed)
        R = self.reread_term(s, p)
        return replace(p, summary_fixed=p.summary_fixed + R) if R > 0 else p

    def reread_value(self, s: SessionState) -> float | None:
        """The re-read term currently inside C, for the decision log."""
        if not self.cfg.reread_charge or self.cfg.mode in ("off", "fixed") or self.L0(s) is None:
            return None
        c = self.cfg
        p = Params(L0=self.L0(s), Lp=self.Lp(s), g=s.g.value, alpha=c.alpha, beta=c.beta, phi=c.phi,
                   m=c.m, gamma=self.gamma(s), summary_out=self.summary_out(s), summary_fixed=c.summary_fixed)
        return self.reread_term(s, p)

    def rent_base(self, s: SessionState, prompt: float) -> float:
        base = s.window_start_prompt if s.window_start_prompt is not None else prompt
        if self.cfg.rent_base == "handoff":
            L0 = self.L0(s)
            if L0 is not None:
                base = max(base, L0 + self.Lp(s))
        if self.cfg.rent_net_reread:
            base += s.rr_growth
        return base

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
        if c.formula_base == "window_start" and s.window_start_prompt is not None:
            # K* does not depend on P_s, so shift L* and the floor from the modeled start to the
            # realized one (L* = P_s + g (K* - 1)).
            shift = s.window_start_prompt - (p.L0 + self.Lp(s))
            L += shift
            floor += shift
            reason += "_ws"
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
            s.rr_growth = 0.0
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
            if s.window_idx >= 2:                    # the finished window itself followed a compaction
                s.rr_w_obs.append(s.rr_growth)
            s.window_start_prompt = prompt
            s.window_turns = 1
            s.rent = 0.0
            s.rr_growth = 0.0
        elif s.last_main_prompt is not None:
            s.g.add(prompt - s.last_main_prompt)
            s.window_turns += 1
            if s.rr_growth_next:
                s.rr_growth += max(prompt - s.last_main_prompt, 0.0)
            s.rent += max(prompt - self.rent_base(s, prompt), 0.0)
        s.expect_boundary = False
        s.rr_growth_next = False
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
                excess_next = max(prompt + c.lookahead * g_hat - self.rent_base(s, prompt), 0.0)
                L0 = self.L0(s) or 0.0
                if (rent + excess_next >= C and s.window_turns >= self.k_min()
                        and prompt > L0 + self.Lp(s)):
                    compact, reason = True, "balance"
                elif (c.hard_cap is not None and prompt + c.lookahead * g_hat >= c.hard_cap
                        and prompt > L0 + self.Lp(s)):
                    compact, reason = True, "hard_cap"
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
                     rent=rent, C=C, reread=self.reread_value(s),
                     rr_growth=s.rr_growth if c.mode == "balance" else None)
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
