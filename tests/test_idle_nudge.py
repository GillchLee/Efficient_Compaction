"""patch 05: HB_IDLE_NUDGE=N appends the no-operator notice after N consecutive no-command turns."""
import pytest

harbor = pytest.importorskip("harbor")
from _profiles import needs_lhtb_fork  # noqa: E402

pytestmark = needs_lhtb_fork
from harbor.agents.terminus_2.terminus_2 import Terminus2  # noqa: E402


def _agent():
    a = Terminus2.__new__(Terminus2)
    a._idle_empty_turns = 0
    return a


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("HB_IDLE_NUDGE", raising=False)
    a = _agent()
    assert all(a._idle_nudge([]) is None for _ in range(10))
    assert a._idle_empty_turns == 10            # still counted, never shown


def test_nudge_after_n_and_reset(monkeypatch):
    monkeypatch.setenv("HB_IDLE_NUDGE", "3")
    a = _agent()
    assert a._idle_nudge([]) is None
    assert a._idle_nudge([]) is None
    text = a._idle_nudge([])
    assert text and "no human operator" in text and "3 consecutive" in text
    assert "4 consecutive" in a._idle_nudge([])   # keeps nudging while idle
    assert a._idle_nudge([{"keystrokes": "ls\n"}]) is None
    assert a._idle_empty_turns == 0
    assert a._idle_nudge([]) is None              # count restarts


@pytest.mark.parametrize("value", ["", "0", "abc"])
def test_invalid_or_zero_disables(monkeypatch, value):
    monkeypatch.setenv("HB_IDLE_NUDGE", value)
    a = _agent()
    assert all(a._idle_nudge([]) is None for _ in range(5))


def test_no_human_note_off_by_default(monkeypatch):
    monkeypatch.delenv("HB_NO_HUMAN_NOTE", raising=False)
    assert Terminus2._no_human_note() == ""


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_no_human_note_on(monkeypatch, value):
    monkeypatch.setenv("HB_NO_HUMAN_NOTE", value)
    note = Terminus2._no_human_note()
    assert note.startswith("\n\n") and "no human in the loop" in note and "task_complete" in note


def test_nudge_mentions_impossible_case(monkeypatch):
    monkeypatch.setenv("HB_IDLE_NUDGE", "1")
    a = _agent()
    text = a._idle_nudge([])
    assert "seems impossible" in text and "at least one command" in text


def _guard_agent():
    a = _agent()
    a._after_rejection = False
    a._cmds_since_verify = 0
    a._last_verify_monotonic = None
    return a


def test_resubmit_guard_off_by_default(monkeypatch):
    monkeypatch.delenv("HB_RESUBMIT_GUARD", raising=False)
    monkeypatch.delenv("HB_VERIFY_MIN_INTERVAL_SEC", raising=False)
    a = _guard_agent()
    a._after_rejection = True
    a._last_verify_monotonic = 0.0
    assert a._resubmit_blocked(True) is None


def test_resubmit_guard_blocks_only_unchanged_resubmission(monkeypatch):
    monkeypatch.setenv("HB_RESUBMIT_GUARD", "1")
    monkeypatch.delenv("HB_VERIFY_MIN_INTERVAL_SEC", raising=False)
    a = _guard_agent()
    assert a._resubmit_blocked(True) is None         # first submission always reaches the verifier
    a._after_rejection = True                        # set by resume_after_verifier_rejection
    assert a._resubmit_blocked(False) is None        # ordinary turn
    assert a._resubmit_blocked(True) == "unchanged"
    a._cmds_since_verify = 1                         # a command ran since the rejection
    assert a._resubmit_blocked(True) is None
    assert "nothing has changed" in a._resubmit_block_notice("unchanged")


def test_verify_min_interval(monkeypatch):
    import time as _t
    monkeypatch.delenv("HB_RESUBMIT_GUARD", raising=False)
    monkeypatch.setenv("HB_VERIFY_MIN_INTERVAL_SEC", "300")
    a = _guard_agent()
    a._after_rejection = True
    a._cmds_since_verify = 1
    a._last_verify_monotonic = _t.monotonic()        # verifier just ran
    assert a._resubmit_blocked(True) == "interval"
    note = a._resubmit_block_notice("interval")
    assert "once every 300 seconds" in note and "do not idle" in note
    a._last_verify_monotonic = _t.monotonic() - 301  # interval elapsed
    assert a._resubmit_blocked(True) is None


def test_giveup_after(monkeypatch):
    a = _guard_agent()
    a._noop_turns = 50
    monkeypatch.delenv("HB_GIVEUP_AFTER", raising=False)
    a._after_rejection = True
    assert a._gave_up() is False                      # off by default
    monkeypatch.setenv("HB_GIVEUP_AFTER", "20")
    a._after_rejection = False
    assert a._gave_up() is False                      # never before the first verification
    a._after_rejection = True
    a._noop_turns = 19
    assert a._gave_up() is False
    a._noop_turns = 20
    assert a._gave_up() is True
