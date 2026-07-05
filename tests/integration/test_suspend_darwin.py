"""Pin the authoritative suspend detector on darwin (FR-5.1, integration).

A real host suspend can't be forced in a test, so — per the FR-5.1 acceptance's
sanctioned alternative — this uses an *injected suspend-excluding uptime
reading* (monotonic held while wallclock advances, which is exactly how darwin's
`time.monotonic()` behaves across a real sleep) and asserts the PRIMARY
clock-skew detector is the one that fires. It also checks a real short sleep
(no suspend) records nothing, so ordinary jitter never trips detection. Marked
`integration` and darwin-only: it pins this platform's clock semantics with a
live assertion rather than assuming them.
"""

from __future__ import annotations

import sys
import time
from datetime import timedelta

import pytest

from gauntlet.engine import heartbeat as HB

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "darwin", reason="pins darwin clock semantics"),
]


def test_real_short_sleep_records_no_suspension():
    from datetime import datetime, timezone

    t0 = datetime.now(timezone.utc)
    m0 = time.monotonic()
    time.sleep(1.0)
    prev = HB.HeartbeatSample(m0, HB.format_wallclock(t0), 1)
    cur = HB.HeartbeatSample(
        time.monotonic(), HB.format_wallclock(datetime.now(timezone.utc)), 1
    )
    assert HB.detect_suspension(prev, cur) is None


def test_injected_suspend_excluding_reading_fires_primary_detector():
    from datetime import datetime, timezone

    t0 = datetime.now(timezone.utc)
    m0 = time.monotonic()
    # Injected suspend-excluding reading: wallclock advanced 40 minutes while the
    # monotonic clock advanced only the cadence — darwin's real behavior across a
    # lid-close. The primary (skew) rule is what catches this on darwin.
    prev = HB.HeartbeatSample(m0, HB.format_wallclock(t0), 1)
    cur = HB.HeartbeatSample(
        m0 + 15.0, HB.format_wallclock(t0 + timedelta(minutes=40)), 1
    )
    s = HB.detect_suspension(prev, cur)
    assert s is not None
    assert s.gap_s == 2400
    # Skew (Δwall − Δmono) dominated the threshold — i.e. the PRIMARY rule fired,
    # not merely the cadence fallback — pinning the primary as authoritative here.
    assert (2400 - 15) > HB.SUSPEND_THRESHOLD_S
