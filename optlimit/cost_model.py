"""Per-turn input cost of context compaction and the cost-optimal threshold.

Notation follows cost_model_derivation2.pdf and the "B" extension (summary
call cost), all in units of the cache-read price u:

    read  : 1          (cache read)
    write : alpha      (cache write / uncached prefill)
    output: beta       (summary output)

A window starts at a compaction turn with prompt L0 + Lp and grows by g per
turn until the prompt about to be sent exceeds L (the threshold), so

    K  = (L - L0 - Lp) / g + 1            turns per window
    f_A(L) = (L0 + Lp + L) / 2 + (alpha - 1) * (g + (Lp - g) / K)      (PDF eq. 3)

The summary call(s) of one compaction read the full context (~L) `m` times
at price multiplier `gamma` (1 = cache read, alpha = uncached) and emit
`summary_out` tokens at beta, plus `summary_fixed` tokens of instructions:

    f_B(L) = f_A(L) + (m * gamma * L + beta * summary_out + summary_fixed) / K

With m = gamma = 1, summary_out = Lp, summary_fixed = 0 this is exactly the
pasted "B" derivation:  f_B = f_A + (L + beta * Lp) / K.

Lp may depend on L: Lp = c + phi * L (PDF assumption 4).  phi = 0 gives the
closed forms; phi > 0 is handled numerically and, for the pasted special
case, by the closed form `optimal_L_closed_phi`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable


@dataclass(frozen=True)
class Prices:
    """$/Mtok. `uncached` is the price of a plain (non-cache-write) input token;
    Anthropic charges 1.0x for it and 1.25x for a cache write, OpenAI-style
    APIs have no separate write price (uncached == write)."""

    read: float
    write: float
    output: float
    uncached: float | None = None

    @property
    def alpha(self) -> float:
        return self.write / self.read

    @property
    def beta(self) -> float:
        return self.output / self.read

    def cost_usd(self, read: float, write: float, output: float, uncached: float = 0.0) -> float:
        u = self.uncached if self.uncached is not None else self.write
        return (read * self.read + write * self.write + output * self.output + uncached * u) / 1e6


@dataclass(frozen=True)
class Params:
    L0: float            # fixed prefix, cached after compaction (system prompt, tools, task)
    Lp: float            # prompt right after compaction when phi == 0; the constant c when phi > 0
    g: float             # prompt growth per turn
    alpha: float         # write / read
    beta: float = 0.0    # output / read; 0 ignores the summary output (model A + reads only)
    phi: float = 0.0     # Lp = Lp + phi * L
    m: float = 0.0       # full-context reads per compaction by the summary call(s); 0 = model A
    gamma: float = 1.0   # price multiplier of those reads (1 cached, alpha uncached)
    summary_out: float | None = None  # summary output tokens; None -> = Lp (pasted derivation)
    summary_fixed: float = 0.0        # fixed extra input per compaction (instructions), at read price

    @property
    def model_b(self) -> bool:
        return self.m > 0 or self.beta > 0 or self.summary_fixed > 0

    def lp_at(self, L: float) -> float:
        return self.Lp + self.phi * L

    def summary_out_at(self, L: float) -> float:
        return self.lp_at(L) if self.summary_out is None else self.summary_out

    def pasted_b(self) -> "Params":
        """The pasted derivation's assumptions: one cached read of L, output = Lp."""
        return replace(self, m=1.0, gamma=1.0, summary_out=None, summary_fixed=0.0)


def turns_per_window(L: float, p: Params) -> float:
    return ((1 - p.phi) * L - p.L0 - p.Lp) / p.g + 1


def min_L(p: Params) -> float:
    """Smallest threshold with a one-turn window (K = 1)."""
    return (p.L0 + p.Lp) / (1 - p.phi)


def f_A(L: float, p: Params) -> float:
    K = turns_per_window(L, p)
    if K < 1:
        return math.inf
    Lp = p.lp_at(L)
    return (p.L0 + Lp + L) / 2 + (p.alpha - 1) * (p.g + (Lp - p.g) / K)


def f_summary(L: float, p: Params) -> float:
    """Extra per-turn cost of the summary call(s)."""
    K = turns_per_window(L, p)
    if K < 1:
        return math.inf
    return (p.m * p.gamma * L + p.beta * p.summary_out_at(L) + p.summary_fixed) / K


def f(L: float, p: Params) -> float:
    """Average input-side cost per turn, in units of the cache-read price."""
    return f_A(L, p) + (f_summary(L, p) if p.model_b else 0.0)


def f_no_compaction(T: int, p: Params) -> float:
    """Per-turn cost when the task runs T turns without compacting (linear growth
    from L0 + Lp... the first window has no summary, so the prompt starts at L0)."""
    A = T * p.L0 + p.g * T * (T - 1) / 2
    W = p.L0 + p.g * (T - 1)
    return (A + (p.alpha - 1) * W) / T


# ---------------------------------------------------------------- closed forms

def order_cost(p: Params) -> float:
    """C in y* = sqrt(2 g C): the per-compaction cost that competes with re-reading.

    Model A: (alpha - 1)(Lp - g)                                  (PDF eq. 5)
    Model B: + m*gamma*(L0 + Lp - g) + beta*O_s + summary_fixed
    which for m = gamma = 1, O_s = Lp is alpha(Lp - g) + L0 + beta Lp   (pasted C_B).
    """
    C = (p.alpha - 1) * (p.Lp - p.g)
    if p.model_b:
        C += p.m * p.gamma * (p.L0 + p.Lp - p.g) + p.beta * p.summary_out_at(0.0) + p.summary_fixed
    return C


def optimal_L_closed(p: Params) -> float | None:
    """phi = 0 closed form: L* = L0 + Lp - g + sqrt(2 g C).  None if no interior optimum."""
    if p.phi != 0:
        raise ValueError("closed form is for phi = 0; use optimal_L_closed_phi or optimal_L")
    if p.summary_out is None and p.model_b and p.phi == 0:
        pass  # summary_out_at(0) == Lp, fine
    C = order_cost(p)
    if C <= 0:
        return None
    return p.L0 + p.Lp - p.g + math.sqrt(2 * p.g * C)


def optimal_L_closed_phi(p: Params) -> float | None:
    """Pasted section 5 (Lp = c + phi L, one cached read of L, output beta*Lp):

        X   = c - g + phi L0
        X_B = (1 + beta phi)(L0 + c - g) + (1 - phi) beta c
        D*^2 = 2 g [(alpha - 1) X + X_B] / (1 + phi)        (X_B term only in model B)
        L*  = (L0 + c - g + D*) / (1 - phi)
    """
    if p.model_b and not (p.m == 1 and p.gamma == 1 and p.summary_out is None and p.summary_fixed == 0):
        raise ValueError("closed phi form covers only the pasted assumptions (m=gamma=1, O_s=Lp)")
    c, g, L0, a, b, phi = p.Lp, p.g, p.L0, p.alpha, p.beta, p.phi
    X = c - g + phi * L0
    tot = (a - 1) * X
    if p.model_b:
        tot += (1 + b * phi) * (L0 + c - g) + (1 - phi) * b * c
    if tot <= 0:
        return None
    D = math.sqrt(2 * g * tot / (1 + phi))
    return (L0 + c - g + D) / (1 - phi)


# ---------------------------------------------------------------- numeric

def optimal_L(p: Params, lo: float | None = None, hi: float | None = None, step: float = 1.0,
              weight: Callable[[float], float] | None = None) -> float:
    """Numeric minimiser of weight(L) * f(L) on a grid (weight = T(L)/T0, PDF eq. 11).

    Golden-section on the unimodal f, then a local integer grid scan; exact enough
    for thresholds (the curve is flat near the optimum: PDF eq. 6)."""
    lo = min_L(p) + p.g if lo is None else lo
    hi = max(lo + 10 * p.g, 50 * (p.L0 + p.Lp) + 200 * p.g) if hi is None else hi
    w = weight or (lambda L: 1.0)
    obj = lambda L: w(L) * f(L, p)
    a, b = lo, hi
    gr = (math.sqrt(5) - 1) / 2
    c, d = b - gr * (b - a), a + gr * (b - a)
    fc, fd = obj(c), obj(d)
    while b - a > 4 * step:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a); fc = obj(c)
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a); fd = obj(d)
    best = min((obj(x), x) for x in _grid(a, b, step))
    return best[1]


def _grid(a: float, b: float, step: float):
    x = a
    while x <= b:
        yield x
        x += step


def best_threshold(p: Params) -> float:
    """Closed form when available, numeric otherwise; clamped to K >= 1."""
    L = None
    if p.phi == 0:
        L = optimal_L_closed(p)
    if L is None:
        L = optimal_L(p)
    return max(L, min_L(p))


# ---------------------------------------------------------------- short tasks (PDF 6a)

def should_compact_now(C: float, R: float, p: Params) -> bool:
    """With current prompt C and R turns remaining, compact iff the reading saved
    over the remaining turns exceeds the one-off cost of compacting:

        R (C - L0 - Lp) > (alpha - 1)(Lp - g) + m gamma C + beta O_s + summary_fixed
    """
    Lp = p.lp_at(C)
    rhs = (p.alpha - 1) * (Lp - p.g)
    if p.model_b:
        rhs += p.m * p.gamma * C + p.beta * p.summary_out_at(C) + p.summary_fixed
    return R * (C - p.L0 - Lp) > rhs


def window_cost_breakdown(L: float, p: Params) -> dict:
    """Per-turn components for reports."""
    K = turns_per_window(L, p)
    Lp = p.lp_at(L)
    out = {
        "L": L, "K": K, "Lp": Lp,
        "avg_prompt": (p.L0 + Lp + L) / 2,
        "write_premium": (p.alpha - 1) * (p.g + (Lp - p.g) / K),
        "summary_read": p.m * p.gamma * L / K if p.model_b else 0.0,
        "summary_output": p.beta * p.summary_out_at(L) / K if p.model_b else 0.0,
        "summary_fixed": p.summary_fixed / K if p.model_b else 0.0,
    }
    out["f"] = f(L, p)
    return out
