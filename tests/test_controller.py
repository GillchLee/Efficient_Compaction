from optlimit.controller import ControllerConfig, LimitController
from optlimit.estimator import GEstimator
from optlimit import cost_model as cm


def test_g_estimator_shrinks_to_prior_then_data():
    g = GEstimator(prior=1000, prior_weight=2)
    assert g.value == 1000
    for _ in range(20):
        g.add(2000)
    assert 1900 < g.value < 2000
    g.add(10 ** 6)  # clipped
    assert g.value < 3000


def test_controller_formula_triggers_and_tracks_windows():
    cfg = ControllerConfig(mode="formula", Lp=8300, alpha=12.5, g_prior=1400, g_prior_weight=1, min_window_turns=2)
    ctl = LimitController(cfg)
    key = "s"
    P = 900.0
    d = ctl.on_prompt(key, "main", P)
    assert d.L0 == 900 and not d.compact_now and d.limit is not None
    expect = cm.best_threshold(cm.Params(L0=900, Lp=8300, g=1400, alpha=12.5))
    assert abs(d.limit - expect) < 1
    fired = None
    for t in range(200):
        P += 1400
        d = ctl.on_prompt(key, "main", P)
        if d.compact_now:
            fired = (t, P, d.limit)
            break
    assert fired is not None
    assert fired[1] + 1400 >= fired[2] > fired[1]
    # harness compacts: summary call then a small prompt -> new window, g unaffected by the drop
    ctl.note_kind(key, "summary")
    d = ctl.on_prompt(key, "summary", P)
    assert d.reason == "summary"
    d = ctl.on_prompt(key, "main", 9000.0)
    assert d.window_start and d.window_idx == 1
    s = ctl.session(key)
    assert s.n_compactions == 1 and not s.pending_compact
    assert abs(s.g.value - 1400) < 1


def test_boundary_detected_from_prompt_drop_without_hint():
    ctl = LimitController(ControllerConfig(mode="off", g_prior=1000))
    for P in (1000, 2000, 3000, 30000, 31000):
        ctl.on_prompt("k", "main", float(P))
    d = ctl.on_prompt("k", "main", 9000.0)
    assert d.window_start


def test_no_prior_waits_for_first_growth_then_uses_observed_mean():
    ctl = LimitController(ControllerConfig(mode="formula", Lp=4400, alpha=10, beta=50, m=2, summary_out=4200,
                                           g_prior=5000, g_prior_weight=0, min_window_turns=2))
    d = ctl.on_prompt("k", "main", 1300.0)
    assert d.limit is None and d.reason == "no_g" and not d.compact_now
    d = ctl.on_prompt("k", "main", 2000.0)          # first observed increment: 700
    assert d.limit is not None and abs(d.g_hat - 700) < 1e-6
    expect = cm.best_threshold(cm.Params(L0=1300, Lp=4400, g=700, alpha=10, beta=50, m=2, summary_out=4200))
    assert abs(d.limit - expect) < 1
    P, fired = 2000.0, None
    for t in range(500):
        P += 700
        d = ctl.on_prompt("k", "main", P)
        if d.compact_now:
            fired = (P, d.limit)
            break
    assert fired is not None and fired[0] + 700 >= fired[1] > fired[0] - 700


def test_fixed_mode_lookahead():
    ctl = LimitController(ControllerConfig(mode="fixed", fixed_limit=20000, Lp=1000, g_prior=1000, g_prior_weight=1000,
                                           lookahead=1.0))
    ctl.on_prompt("k", "main", 500.0)
    assert not ctl.on_prompt("k", "main", 18500.0).compact_now
    assert ctl.on_prompt("k", "main", 19100.0).compact_now


def test_update_prices_moves_the_limit_and_is_recorded():
    cfg = ControllerConfig(mode="formula", Lp=3203, L0_mode="fixed", L0_fixed=1258, alpha=10, beta=50,
                           m=2.178, gamma=1.508, summary_out=4242, g_prior=717, g_prior_weight=5)
    ctl = LimitController(cfg)
    d1 = ctl.on_prompt("s", "main", 2000.0)
    assert d1.alpha == 10 and d1.beta == 50
    ctl.update_prices(25, 100, note="sonnet-5-5 ratios")
    d2 = ctl.on_prompt("s", "main", 2700.0)
    assert d2.alpha == 25 and d2.beta == 100
    # higher write/output premiums make each compaction dearer -> larger window
    assert d2.limit > d1.limit * 1.2
    assert ctl.price_updates[-1]["note"] == "sonnet-5-5 ratios"
    # gamma follows alpha only when learned from an uncached fraction; fixed otherwise
    assert d2.gamma == 1.508


def test_learned_terms_track_observed_compactions():
    base = dict(mode="formula", Lp=3203, L0_mode="fixed", L0_fixed=1258, alpha=10, beta=50,
                m=2.178, gamma=1.508, summary_out=4242, g_prior=717, g_prior_weight=5, learn_weight=1.0)
    fixed = LimitController(ControllerConfig(**base))
    learn = LimitController(ControllerConfig(**base, learn_lp=True, learn_summary_out=True, learn_gamma=True))
    for ctl in (fixed, learn):
        P = 2000.0
        ctl.on_prompt("s", "main", P)
        for _ in range(3):
            # run a window up to the limit, then a compaction whose hand-off is 10K (not 3.2K)
            for _ in range(200):
                P += 800
                d = ctl.on_prompt("s", "main", P)
                if d.compact_now:
                    break
            # summary calls: 2.178 reads of ~P, half of it uncached, 9000 output tokens
            ctl.note_summary_usage("s", prompt=2.178 * P, cached=0.5 * 2.178 * P, completion=9000)
            ctl.note_kind("s", "summary")
            P = 1258 + 10000
            d = ctl.on_prompt("s", "main", P, cached=1258)
            assert d.window_start
    df, dl = fixed.session("s").last_decision, learn.session("s").last_decision
    assert df.Lp == 3203 and df.summary_out == 4242 and df.gamma == 1.508
    assert 8000 < dl.Lp < 10000 and 7500 < dl.summary_out < 9000
    assert abs(dl.gamma - (1 + 9 * ((0.0564 + 3 * 0.5) / 4))) < 0.05   # prior uncached 5.6% w=1, then 3 x 50%
    assert dl.limit > df.limit * 1.3


def test_reacquisition_floor_raises_small_windows():
    base = dict(mode="formula", Lp=3203, L0_mode="fixed", L0_fixed=1258, alpha=1.5, beta=2.5,
                m=2.178, gamma=1.03, summary_out=4242, g_prior=1000, g_prior_weight=5)
    plain = LimitController(ControllerConfig(**base)).on_prompt("s", "main", 2000.0)
    floored = LimitController(ControllerConfig(**base, reacq_turns=1.5, reacq_eps=0.1)).on_prompt("s", "main", 2000.0)
    # cheap prices (GLM-like) give a ~10K window; 1.5 re-acquired turns per compaction at <=10% => K >= 15
    assert plain.limit < 1258 + 3203 + 1000 * 14
    assert floored.limit >= 1258 + 3203 + 1000 * 14 and floored.reason == "floor_min_window"
    # at Luna prices the charge for re-acquired turns moves L* up a little but the floor does not bind
    luna = dict(base, alpha=10, beta=50, gamma=1.508)
    a = LimitController(ControllerConfig(**luna)).on_prompt("s", "main", 2000.0)
    b = LimitController(ControllerConfig(**luna, reacq_turns=1.5)).on_prompt("s", "main", 2000.0)
    assert b.reason == "formula" and 1.02 < b.limit / a.limit < 1.2


def _run(cfg, incs, P0=2000.0):
    """Feed a prompt sequence; return the turn indices at which the controller fired."""
    ctl = LimitController(cfg)
    fired = []
    P = P0
    ctl.on_prompt("s", "main", P)
    boundary = False
    for t, inc in enumerate(incs, start=1):
        P += inc
        d = ctl.on_prompt("s", "main", P, window_start_hint=boundary)
        boundary = False
        if d.compact_now:
            fired.append(t)
            ctl.note_kind("s", "summary")
            P = 1258 + 3203
            d = ctl.on_prompt("s", "main", P, cached=1258)
            assert d.window_start
    return fired


def test_balance_rule_matches_formula_for_constant_growth_and_reacts_to_bursts():
    base = dict(Lp=3203, L0_mode="fixed", L0_fixed=1258, alpha=10, beta=50, m=2.178, gamma=1.508,
                summary_out=4242, g_prior=1000, g_prior_weight=1000, lookahead=1)
    const = [1000.0] * 120
    f_fired = _run(ControllerConfig(mode="formula", **base), const)
    b_fired = _run(ControllerConfig(mode="balance", **base), const)
    assert len(f_fired) >= 2 and len(b_fired) >= 2
    # same window length (+-1 turn) once both start from L0 + Lp: the balance rule reproduces
    # the closed-form optimum (the very first window starts lower, so compare the second)
    assert abs((f_fired[1] - f_fired[0]) - (b_fired[1] - b_fired[0])) <= 1
    # bursty growth: a 15K tool output early in the window. The level rule (formula) fires as
    # soon as the prompt passes L*, i.e. right after the burst; the balance rule waits until
    # the rent actually paid for re-reading it reaches C (DTAP: no bet on the future, 2-competitive)
    burst = [1000.0] * 3 + [15000.0] + [1000.0] * 116
    f_b = _run(ControllerConfig(mode="formula", **base), burst)
    b_b = _run(ControllerConfig(mode="balance", **base), burst)
    assert f_b[0] < b_b[0] < f_fired[0]
    # the decision record carries rent and C
    ctl = LimitController(ControllerConfig(mode="balance", **base))
    ctl.on_prompt("s", "main", 2000.0)
    d = ctl.on_prompt("s", "main", 3000.0)
    assert d.rent == 1000.0 and d.C is not None and d.C > 200000 and d.reason == "balance_ref"
