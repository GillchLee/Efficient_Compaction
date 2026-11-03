"""Online estimate of g (prompt growth per turn) from the prompt sizes a session sends.

The formula uses the *mean* growth inside a window (K = (L - L0 - Lp) / g + 1 is
an identity for the window mean), so the default estimate is a shrunken mean:

    g_hat = (n0 * g_prior + sum(dP)) / (n0 + n)

where the prior comes from configs/priors.json (per harness/model) with weight
n0 (pseudo-turns).  Growth is measured only across consecutive main-loop turns
of the same window (no compaction between), and only when the client re-sent
the same conversation grown by one step (the proxy checks the prefix hash).

Heavy tails: measured pooled CV of g is 1-2, so `clip` drops a turn whose
growth is above `clip_factor` x the running estimate from the *mean* path
(a single 40K tool output would otherwise double g_hat for the next window);
the clipped values still count in the median path.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass
class GEstimator:
    prior: float
    prior_weight: float = 5.0
    mode: str = "mean"          # mean | median | ema
    ema_alpha: float = 0.2
    clip_factor: float | None = 10.0
    min_g: float = 50.0
    deltas: list[float] = field(default_factory=list)
    _sum: float = 0.0
    _n: int = 0
    _ema: float | None = None

    def add(self, delta: float) -> None:
        if delta <= 0:
            return
        self.deltas.append(delta)
        cur = self.value
        if self.clip_factor and delta > self.clip_factor * cur:
            delta_mean = self.clip_factor * cur
        else:
            delta_mean = delta
        self._sum += delta_mean
        self._n += 1
        self._ema = delta_mean if self._ema is None else (1 - self.ema_alpha) * self._ema + self.ema_alpha * delta_mean

    @property
    def n(self) -> int:
        return self._n

    @property
    def value(self) -> float:
        if self.mode == "median" and self.deltas:
            obs = statistics.median(self.deltas)
            w = self._n / (self._n + self.prior_weight)
            v = (1 - w) * self.prior + w * obs
        elif self.mode == "ema" and self._ema is not None:
            w = self._n / (self._n + self.prior_weight)
            v = (1 - w) * self.prior + w * self._ema
        else:
            v = (self.prior_weight * self.prior + self._sum) / (self.prior_weight + self._n)
        return max(v, self.min_g)

    def snapshot(self) -> dict:
        return {"g_hat": self.value, "n": self._n, "prior": self.prior, "prior_weight": self.prior_weight,
                "mode": self.mode, "median": statistics.median(self.deltas) if self.deltas else None}
