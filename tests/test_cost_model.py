import math

import pytest

from optlimit import cost_model as cm


def base(**kw):
    d = dict(L0=900, Lp=8300, g=1400, alpha=12.5)
    d.update(kw)
    return cm.Params(**d)


def test_model_a_closed_form_matches_pdf_eq5():
    p = base()
    L = cm.optimal_L_closed(p)
    expect = p.L0 + p.Lp - p.g + math.sqrt(2 * (p.alpha - 1) * p.g * (p.Lp - p.g))
    assert L == pytest.approx(expect)


def test_model_a_closed_vs_numeric():
    p = base()
    assert cm.optimal_L(p, step=1.0) == pytest.approx(cm.optimal_L_closed(p), abs=5)


def test_pasted_model_b_closed_form():
    # L_B = L0 + Lp - g + sqrt(2g[alpha(Lp-g) + L0 + beta Lp])
    p = base(beta=50.0).pasted_b()
    L = cm.optimal_L_closed(p)
    expect = p.L0 + p.Lp - p.g + math.sqrt(2 * p.g * (p.alpha * (p.Lp - p.g) + p.L0 + p.beta * p.Lp))
    assert L == pytest.approx(expect)
    assert cm.optimal_L(p) == pytest.approx(L, abs=5)
    # summary cost makes compaction dearer -> larger threshold
    assert L > cm.optimal_L_closed(base())


def test_f_B_equals_f_A_plus_summary_term():
    p = base(beta=50.0).pasted_b()
    L = 40000.0
    K = cm.turns_per_window(L, p)
    assert cm.f(L, p) == pytest.approx(cm.f_A(L, p) + (L + p.beta * p.Lp) / K)


def test_phi_closed_form_reduces_to_phi0_and_matches_numeric():
    p0 = base(beta=50.0).pasted_b()
    assert cm.optimal_L_closed_phi(p0) == pytest.approx(cm.optimal_L_closed(p0))
    p = cm.Params(L0=900, Lp=4000, g=1400, alpha=12.5, beta=50.0, phi=0.1, m=1, gamma=1)
    Lc = cm.optimal_L_closed_phi(p)
    assert cm.optimal_L(p) == pytest.approx(Lc, abs=10)
    pa = cm.Params(L0=900, Lp=4000, g=1400, alpha=12.5, phi=0.1)
    assert cm.optimal_L(pa) == pytest.approx(cm.optimal_L_closed_phi(pa), abs=10)


def test_general_summary_terms_change_optimum_monotonically():
    p1 = base(beta=50.0, m=1, gamma=1, summary_out=3000)
    p2 = base(beta=50.0, m=2, gamma=12.5, summary_out=3000)
    assert cm.optimal_L_closed(p2) > cm.optimal_L_closed(p1)
    assert cm.optimal_L(p2) == pytest.approx(cm.optimal_L_closed(p2), abs=5)


def test_no_interior_optimum_when_Lp_below_g():
    p = base(Lp=1000, g=1400)
    assert cm.optimal_L_closed(p) is None
    assert cm.best_threshold(p) >= cm.min_L(p)


def test_short_task_rule():
    p = base(beta=50.0).pasted_b()
    # far from the end many turns remain -> compact when prompt is big
    assert cm.should_compact_now(C=60000, R=100, p=p)
    # one turn left -> never worth it
    assert not cm.should_compact_now(C=60000, R=1, p=p)


def test_deviation_cost_quadratic_near_optimum():
    p = base()
    L = cm.optimal_L_closed(p)
    y = L - p.L0 - p.Lp + p.g
    for dy in (2000, 5000):
        fl = cm.f(L + dy, p) - cm.f(L, p)
        assert fl == pytest.approx(dy ** 2 / (2 * (y + dy)), rel=1e-6)  # PDF eq. 6
