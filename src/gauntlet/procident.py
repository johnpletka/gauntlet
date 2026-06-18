"""ProcessIdentity — PID-reuse-safe process identity (P3, FR-6.4/FR-7.2).

A bare ``os.kill(pid, 0)`` proves a *pid* is live, not that it is the *same*
process we launched: the OS recycles pids, so a reused pid passes the signal
probe while being an entirely different program. To defeat that, we pin a pid to
its **OS process-creation time** — fixed for a process's whole lifetime — and
treat a pid as "our process" only when both the signal probe **and** an exact
creation-time match agree (FR-7.2).

This single helper is the **only** producer of these values, used identically at
launch (write into ``.serve/job.json`` and ``.driving.lock``) and at re-check
(lock reclaim in P3, server re-attach in P4), so a value captured one place is
always comparable to one read another place — never across platforms or units.

The value is a structured, platform-tagged, integer-normalised record so the
comparison is **exact equality (tolerance 0)**:

- **Linux:** ``/proc/<pid>/stat`` field 22 (``starttime``), integer clock ticks
  since boot — ``unit="boot_ticks"``. Parsed by splitting on the **last** ``)``
  first, since the ``comm`` field may itself contain spaces and parens.
- **macOS/BSD (``darwin``):** ``ps -o lstart= -p <pid>`` parsed into integer
  epoch seconds (``unit="epoch_seconds"``). The subprocess runs with
  ``LC_ALL=C``/``LANG=C`` so the output is the fixed C-locale form
  (``"Wed Jun 17 09:04:21 2026"``), parsed with a pinned ``strptime`` format in
  the local timezone. ``lstart`` granularity is 1 second; since a process's
  start time is fixed, 1-second precision is *exact* for identity, not fuzzy.

Any unreadable ``/proc`` entry, missing/locale-unexpected ``ps`` output,
unparseable timestamp, or **unsupported platform** yields ``None`` — treated as
*unverifiable* and mapped to fail-closed by every caller (a ``None`` recorded or
re-read identity is never a re-attach / never a stale-reclaim-into-live).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

UNIT_BOOT_TICKS = "boot_ticks"
UNIT_EPOCH_SECONDS = "epoch_seconds"

# Pinned C-locale form of `ps -o lstart=` (e.g. "Wed Jun 17 09:04:21 2026").
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


@dataclass(frozen=True)
class ProcessIdentity:
    """A platform-tagged, integer-normalised process-creation identity.

    Two identities denote the *same process* iff ``platform``, ``unit`` and the
    integer ``value`` are **all equal** — exact equality, no tolerance window
    (a non-zero tolerance would weaken PID-reuse safety). Both representations
    are stable integers for a process's lifetime, so no fuzziness is needed.
    """

    platform: str
    value: int
    unit: str

    def to_dict(self) -> dict:
        """Serialise verbatim for ``job.json`` / ``.driving.lock``."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "ProcessIdentity | None":
        """Rehydrate a previously-serialised identity, or ``None`` if malformed.

        A ``None`` / missing / malformed record round-trips to ``None`` so a
        corrupt sidecar is treated as *unverifiable* (fail-closed), never as a
        spurious match.
        """
        if not isinstance(data, dict):
            return None
        try:
            platform = data["platform"]
            value = data["value"]
            unit = data["unit"]
        except (KeyError, TypeError):
            return None
        if not isinstance(platform, str) or not isinstance(unit, str):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return cls(platform=platform, value=value, unit=unit)

    def same_process(self, other: "ProcessIdentity | None") -> bool:
        """True iff ``other`` is the same process — exact equality, tolerance 0.

        ``None`` (an unverifiable / unreadable identity) is never a match.
        """
        if other is None:
            return False
        return (
            self.platform == other.platform
            and self.unit == other.unit
            and self.value == other.value
        )


def _read_linux(pid: int) -> ProcessIdentity | None:
    try:
        stat = (
            open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace")  # noqa: SIM115
            .read()
        )
    except OSError:
        return None
    # comm (field 2) may contain spaces and ')'; split on the LAST ')' so the
    # remaining fields are unambiguous. starttime is field 22 → index 19 after
    # the comm-closing ')'.
    try:
        rest = stat.rsplit(")", 1)[1]
        fields = rest.split()
        starttime = int(fields[19])
    except (IndexError, ValueError):
        return None
    return ProcessIdentity(platform="linux", value=starttime, unit=UNIT_BOOT_TICKS)


def _read_darwin(pid: int) -> ProcessIdentity | None:
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            # Pin the locale so the date is the fixed C form, independent of the
            # operator's ambient LANG (review F-001).
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None  # no such pid (ps exits non-zero) → unverifiable
    raw = " ".join(proc.stdout.split())  # normalise whitespace (single-digit day)
    if not raw:
        return None
    try:
        parsed = time.strptime(raw, _LSTART_FORMAT)
        epoch = int(time.mktime(parsed))  # local-tz epoch seconds (lstart is local)
    except (ValueError, OverflowError):
        return None
    return ProcessIdentity(platform="darwin", value=epoch, unit=UNIT_EPOCH_SECONDS)


def read_process_identity(pid: int) -> ProcessIdentity | None:
    """The OS process-creation identity of ``pid``, or ``None`` if unobtainable.

    ``None`` covers an unreadable ``/proc`` entry, missing/locale-unexpected
    ``ps`` output, an unparseable timestamp, **or an unsupported platform**
    (anything other than ``linux``/``darwin``). Windows/others are therefore
    *supported only in fail-closed mode* in v1 — they always yield ``None`` and
    so are always classified unverifiable on re-check.
    """
    if sys.platform.startswith("linux"):
        return _read_linux(pid)
    if sys.platform == "darwin":
        return _read_darwin(pid)
    return None


def process_is_alive(pid: int, recorded: ProcessIdentity | None) -> bool:
    """True iff ``pid`` is live **and** is the same process as ``recorded``.

    The PID-reuse-safe liveness check (FR-7.2): ``os.kill(pid, 0)`` must succeed
    *and* the freshly-read identity must exactly equal ``recorded``. A ``None``
    recorded identity (unverifiable at capture) or a ``None`` fresh read
    (unobtainable now, incl. unsupported platform) both fail closed → not alive,
    so the caller treats the holder as dead/orphaned and reclaims/recovers.
    """
    if recorded is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # pid is gone
    except PermissionError:
        pass  # alive but owned by another user — the identity check decides
    except OSError:
        return False
    return recorded.same_process(read_process_identity(pid))


__all__ = [
    "ProcessIdentity",
    "read_process_identity",
    "process_is_alive",
    "UNIT_BOOT_TICKS",
    "UNIT_EPOCH_SECONDS",
]
