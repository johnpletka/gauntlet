"""process.py honors an active suspend-aware deadline (FR-5.2 wiring).

The deadline path is engaged only while a driver heartbeat is registered
(``heartbeat._active``); otherwise ``run_with_timeout`` keeps its exact
single-``communicate`` / uncapped-``select`` behavior (covered by
``test_process_timeout.py``). These tests register a fake active writer so the
polled deadline path runs, and assert it (a) completes a fast child normally and
(b) still kills a child once the credited deadline lapses.
"""

from __future__ import annotations

import sys
import time

import pytest

from gauntlet.adapters.process import run_with_timeout
from gauntlet.engine import heartbeat as HB


class _FakeWriter:
    """The minimal surface ``build_active_deadline`` reads off the registry."""

    def __init__(self, *, cap_s: float, detected_s: float = 0.0):
        self.credit_cap_s = cap_s
        self._detected = detected_s

    def detected_suspension_s(self) -> float:
        return self._detected


@pytest.fixture
def active_writer():
    """Register a fake active heartbeat for the test, then clear it."""

    def _register(**kwargs):
        HB._active = _FakeWriter(**kwargs)
        return HB._active

    yield _register
    HB._active = None


def test_fast_child_completes_under_active_deadline(active_writer):
    active_writer(cap_s=12 * 3600.0)
    out = run_with_timeout(
        [sys.executable, "-c", "print('done')"], timeout_s=30
    )
    assert out.exit_code == 0
    assert not out.timed_out
    assert "done" in out.stdout


def test_active_deadline_still_kills_a_wedged_child(active_writer):
    # No detected suspension to credit → the deadline behaves like a plain
    # timeout and the polled path kills the sleeping child promptly.
    active_writer(cap_s=12 * 3600.0, detected_s=0.0)
    start = time.monotonic()
    out = run_with_timeout(
        [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(60)"],
        timeout_s=1.0,
    )
    elapsed = time.monotonic() - start
    assert out.timed_out
    assert "partial" in out.stdout
    assert elapsed < 15  # killed promptly, not the full 60s sleep
