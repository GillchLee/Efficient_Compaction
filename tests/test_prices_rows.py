"""configs/prices.json rows resolve to the price ratios the cost model uses (alpha = write/read, beta = output/read)."""
import pytest

from optlimit.controller import config_from_spec, load_prices


@pytest.mark.parametrize("row,alpha,beta", [
    ("gpt-6-luna", 10.0, 50.0),
    ("claude-haiku-5-5", 12.5, 50.0),
    ("claude-sonnet-5-5", 25.0, 100.0),
    ("qwen3.8-flash", 9.375, 29.375),            # Alibaba Cloud, implicit cache
    ("qwen3.8-flash-next", 9.375, 29.375),       # alias of qwen3.8-flash
    ("qwen3.8-flash-explicit", 12.5, 29.375),    # explicit cache (cache_control)
])
def test_price_row_ratios(row, alpha, beta):
    p = load_prices(row)
    assert p.alpha == pytest.approx(alpha, rel=1e-3) and p.beta == pytest.approx(beta, rel=1e-3)


def test_formula_config_takes_ratios_from_the_prices_row():
    c = config_from_spec({"mode": "formula", "prior": "terminus2-gpt-6-luna", "prices": "qwen3.8-flash-next",
                          "lookahead": 1, "g_prior_weight": 0})
    assert (c.alpha, round(c.beta, 3)) == (9.375, 29.375) and c.Lp == 3203.0
