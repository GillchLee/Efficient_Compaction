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


def test_error_mode_reject_once_then_fail_open():
    ctl = LimitController(ControllerConfig(mode="fixed", fixed_limit=5000, Lp=500, g_prior=1000, max_consecutive_rejects=2))
    ctl.on_prompt("k", "main", 1000.0)
    ctl.on_prompt("k", "main", 4500.0)
    assert ctl.is_pending("k")
    assert ctl.should_reject("k", "main")
    assert ctl.should_reject("k", "main")
    assert not ctl.should_reject("k", "main")  # fail open after 2
    assert not ctl.is_pending("k")


def test_fixed_mode_lookahead():
    ctl = LimitController(ControllerConfig(mode="fixed", fixed_limit=20000, Lp=1000, g_prior=1000, g_prior_weight=1000,
                                           lookahead=1.0))
    ctl.on_prompt("k", "main", 500.0)
    assert not ctl.on_prompt("k", "main", 18500.0).compact_now
    assert ctl.on_prompt("k", "main", 19100.0).compact_now
