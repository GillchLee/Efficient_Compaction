"""Which environment profile the tests run in (setup.sh --profile): the patched LHTB Harbor fork
(./.venv) or stock Harbor (./.venv-h25). Fork-only tests skip themselves on stock Harbor."""
import pytest


def is_lhtb_fork() -> bool:
    try:
        from harbor.agents.terminus_2 import terminus_2
    except Exception:
        return False
    # patches/lhtb-7ca0cac/01-usage-tracking.patch adds Chat.last_usage; the fork also has the
    # same-conversation continuation hook
    return hasattr(terminus_2.Terminus2, "resume_after_verifier_rejection")


needs_lhtb_fork = pytest.mark.skipif(not is_lhtb_fork(), reason="needs the patched LHTB Harbor fork (setup.sh --profile lhtb)")
