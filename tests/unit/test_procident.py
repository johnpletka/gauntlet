"""ProcessIdentity contract (P3, FR-6.4/FR-7.2, review F-001).

The single producer of PID-reuse-safe process identities. These tests pin:
- the host's real branch yields the right ``unit`` and a *stable* integer;
- the macOS parser is locale-pinned (a fixed C-locale ``lstart`` → fixed epoch,
  independent of ambient ``LANG``);
- unobtainable input / unsupported platform → ``None`` (unverifiable);
- comparison is exact equality (tolerance 0), and ``None`` never matches.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from gauntlet import procident
from gauntlet.procident import (
    UNIT_BOOT_TICKS,
    UNIT_EPOCH_SECONDS,
    ProcessIdentity,
    process_is_alive,
    read_process_identity,
)


def test_same_process_is_exact_equality():
    a = ProcessIdentity(platform="linux", value=12345, unit=UNIT_BOOT_TICKS)
    assert a.same_process(ProcessIdentity("linux", 12345, UNIT_BOOT_TICKS))
    # Any field differing → not the same process (tolerance 0).
    assert not a.same_process(ProcessIdentity("linux", 12346, UNIT_BOOT_TICKS))
    assert not a.same_process(ProcessIdentity("darwin", 12345, UNIT_EPOCH_SECONDS))
    assert not a.same_process(ProcessIdentity("linux", 12345, UNIT_EPOCH_SECONDS))
    # None (unverifiable) is never a match.
    assert not a.same_process(None)


def test_roundtrip_dict():
    a = ProcessIdentity(platform="darwin", value=1781757078, unit=UNIT_EPOCH_SECONDS)
    assert ProcessIdentity.from_dict(a.to_dict()) == a
    # Malformed / None round-trips to None (treated as unverifiable).
    assert ProcessIdentity.from_dict(None) is None
    assert ProcessIdentity.from_dict({"platform": "linux"}) is None
    assert ProcessIdentity.from_dict({"platform": "linux", "value": "x", "unit": "u"}) is None


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="read_process_identity is fail-closed (None) off linux/darwin",
)
def test_host_identity_is_stable_and_right_unit():
    ident = read_process_identity(os.getpid())
    assert ident is not None
    if sys.platform.startswith("linux"):
        assert ident.unit == UNIT_BOOT_TICKS
    else:
        assert ident.unit == UNIT_EPOCH_SECONDS
    assert isinstance(ident.value, int)
    # A process's start time is fixed for its lifetime → two reads are identical.
    again = read_process_identity(os.getpid())
    assert again == ident


def test_unobtainable_pid_is_none():
    # A pid that does not exist yields None (unverifiable → fail closed).
    bogus = 2_000_000_000
    assert read_process_identity(bogus) is None


def test_macos_parser_is_locale_pinned(monkeypatch):
    """The darwin parser must produce the same epoch regardless of ambient LANG.

    Feed a fixed C-locale ``lstart`` string through a stubbed ``ps`` and assert
    the parsed epoch matches a direct local-tz ``strptime`` of the same string —
    proving the value does not depend on the environment's locale.
    """
    fixed = "Wed Jun 17 09:04:21 2026"
    expected = int(time.mktime(time.strptime(fixed, "%a %b %d %H:%M:%S %Y")))

    class _Proc:
        returncode = 0
        stdout = fixed + "\n"

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env", {})
        return _Proc()

    monkeypatch.setattr(procident.subprocess, "run", fake_run)
    ident = procident._read_darwin(1234)
    assert ident == ProcessIdentity("darwin", expected, UNIT_EPOCH_SECONDS)
    # The locale is pinned in the subprocess env so the C-locale form is forced.
    assert captured["env"].get("LC_ALL") == "C"
    assert captured["env"].get("LANG") == "C"


def test_macos_parser_handles_single_digit_day(monkeypatch):
    """Double-spaced single-digit day (`Jun  7`) must still parse."""
    fixed = "Sun Jun  7 09:04:21 2026"
    expected = int(time.mktime(time.strptime("Sun Jun 7 09:04:21 2026", "%a %b %d %H:%M:%S %Y")))

    class _Proc:
        returncode = 0
        stdout = fixed + "\n"

    monkeypatch.setattr(procident.subprocess, "run", lambda *a, **k: _Proc())
    assert procident._read_darwin(1234) == ProcessIdentity(
        "darwin", expected, UNIT_EPOCH_SECONDS
    )


def test_macos_parser_failures_are_none(monkeypatch):
    class _Bad:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(procident.subprocess, "run", lambda *a, **k: _Bad())
    assert procident._read_darwin(1234) is None  # ps non-zero (no such pid)

    class _Garbage:
        returncode = 0
        stdout = "not a date\n"

    monkeypatch.setattr(procident.subprocess, "run", lambda *a, **k: _Garbage())
    assert procident._read_darwin(1234) is None  # unparseable


def test_unsupported_platform_is_none(monkeypatch):
    monkeypatch.setattr(procident.sys, "platform", "win32")
    assert read_process_identity(os.getpid()) is None


def test_process_is_alive_fail_closed_on_none():
    # A None recorded identity is unverifiable → never "alive" (FR-7.2).
    assert process_is_alive(os.getpid(), None) is False


def test_process_is_alive_true_for_self():
    ident = read_process_identity(os.getpid())
    if ident is None:  # off-platform host: always fail-closed, nothing to assert
        pytest.skip("identity unobtainable on this platform")
    assert process_is_alive(os.getpid(), ident) is True


def test_process_is_alive_false_on_identity_mismatch():
    # Simulate PID reuse: same pid, different recorded value → not the original.
    ident = read_process_identity(os.getpid())
    if ident is None:
        pytest.skip("identity unobtainable on this platform")
    reused = ProcessIdentity(ident.platform, ident.value + 1, ident.unit)
    assert process_is_alive(os.getpid(), reused) is False
