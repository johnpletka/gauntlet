"""Shared fail-closed lock primitives (P7b, spike §8).

P7b splits one lockfile into three: the retained worktree-global *tree* guard,
the new per-run *driving* lock, and the repo-global git lock under the git
common dir (:mod:`gauntlet.engine.repolock`). All three must agree on exactly
one question — *is the recorded holder provably gone?* — because the answer is
what decides whether a lock may be reclaimed, and a second, subtly-different
copy of that rule is a double-driving bug waiting to happen.

So the record shape, the atomic publish, and the liveness rule live here once
and are consumed by every layer. :mod:`gauntlet.engine.run` re-exports
``LockRecord`` as ``_LockRecord`` (its historical name, which
:mod:`gauntlet.engine.operator` imports) so nothing that reads a lock had to
change when the paths did.

Nothing in this module knows *where* a lock lives; the path policy belongs to
the caller. That is deliberate: P7c relocates the tree guard again, and the
reclaim semantics must not move with it.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.procident import ProcessIdentity, read_process_identity

# The lock filename, identical at every path a drive lock can live (P7b): the
# retained worktree-global guard at `<run_root>/.driving.lock` and the per-run
# lock at `<run_root>/<slug>/<run-id>/.driving.lock`. One name, two scopes —
# see `RunManager._tree_lock_path` / `_run_lock_path` for which is which.
DRIVING_LOCK_NAME = ".driving.lock"

# Bounded retries for an acquire loop racing a stale-lock reclaim, so a
# pathological churn raises rather than spins (fail closed).
LOCK_ACQUIRE_RETRIES = 50


def utc_stamp() -> str:
    """The lock's ``started_at`` stamp.

    Byte-identical to ``run._utc_stamp`` — the format is part of the FR-2.4
    ``since`` field the status payload renders, so it must not drift when the
    record moved here (P7b).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


@dataclass
class LockRecord:
    """The on-disk content of a ``.driving.lock`` (FR-10.5).

    ``nonce`` is a fresh per-acquisition random token; the holder keeps it in
    memory and releases the lock **only** if the file still carries that nonce
    (review F-004), so a holder that was already reclaimed-as-stale can never
    unlink a *new* owner's lock. ``proc_identity`` is the FR-7.2 OS
    process-creation identity (or ``None`` if unobtainable → unverifiable).

    P7b writes the SAME record — one nonce — to both the tree guard and the
    per-run lock of a single acquisition, so a nonce-keyed release (or the
    recovery intent's ``lock_nonce``) identifies both files unambiguously.
    """

    nonce: str
    slug: str
    run_id: str | None
    pid: int
    pgid: int
    started_at: str
    host: str
    proc_identity: dict | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "nonce": self.nonce,
                "slug": self.slug,
                "run_id": self.run_id,
                "pid": self.pid,
                "pgid": self.pgid,
                "started_at": self.started_at,
                "host": self.host,
                "proc_identity": self.proc_identity,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "LockRecord | None":
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            nonce = data["nonce"]
            slug = data["slug"]
            run_id = data.get("run_id")
            started_at = data.get("started_at", "")
            host = data.get("host", "")
            proc_identity = data.get("proc_identity")
            # Type-check the string-typed fields so a malformed lock (a non-string
            # started_at/host/nonce/slug, or a non-dict proc_identity) is treated
            # as malformed → indeterminate driver, never propagated into the
            # status payload as a contract-violating since/host (operator F-003).
            # `bool` is an int subclass but is not a str, so it is rejected here.
            if not all(
                isinstance(v, str) for v in (nonce, slug, started_at, host)
            ):
                return None
            if run_id is not None and not isinstance(run_id, str):
                return None
            if proc_identity is not None and not isinstance(proc_identity, dict):
                return None
            return cls(
                nonce=nonce,
                slug=slug,
                run_id=run_id,
                pid=int(data["pid"]),
                pgid=int(data.get("pgid", data["pid"])),
                started_at=started_at,
                host=host,
                proc_identity=proc_identity,
            )
        except (ValueError, KeyError, TypeError):
            return None


def new_record(slug: str, run_id: str | None) -> LockRecord:
    """A fresh record for THIS process, with a fresh nonce."""
    pid = os.getpid()
    try:
        pgid = os.getpgid(pid)
    except OSError:  # pragma: no cover - platform without process groups
        pgid = pid
    identity = read_process_identity(pid)
    return LockRecord(
        nonce=secrets.token_hex(16),
        slug=slug,
        run_id=run_id,
        pid=pid,
        pgid=pgid,
        started_at=utc_stamp(),
        host=socket.gethostname(),
        proc_identity=identity.to_dict() if identity is not None else None,
    )


# The three things a lockfile can be, kept distinct (review F-002). Collapsing
# `malformed` into `absent` is what let a mutating verb DELETE a lock it could
# not read — including a live holder's, when the file was merely unreadable —
# while `status` was simultaneously calling that same file `indeterminate`. One
# tri-state read, consumed by the operator view, the drive lock and the
# repo-global lock, is what keeps those surfaces from disagreeing (R4).
LOCK_ABSENT = "absent"
LOCK_MALFORMED = "malformed"
LOCK_PRESENT = "present"


def read_lock_state(path: Path) -> tuple[str, LockRecord | None]:
    """Read one lockfile → ``(kind, record)``; the single lock-read primitive.

    * ``LOCK_ABSENT`` — the file is not there. The only kind that may be
      treated as "free": there is no evidence to override.
    * ``LOCK_MALFORMED`` — the file EXISTS but could not be read (``OSError``:
      permissions, I/O error) or could not be parsed. Fail closed: we cannot
      prove who holds it, so it is never reclaimed and never treated as absent.
    * ``LOCK_PRESENT`` — a parsed record; :func:`record_is_live` decides.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return (LOCK_ABSENT, None)
    except OSError:
        return (LOCK_MALFORMED, None)  # exists but unreadable → never reclaimable
    rec = LockRecord.from_json(text)
    if rec is None:
        return (LOCK_MALFORMED, None)
    return (LOCK_PRESENT, rec)


def read_record(path: Path) -> LockRecord | None:
    """The parsed record at ``path``, or ``None`` when it is not readable.

    Convenience for callers that only need the record and treat every
    not-a-record outcome the same way. Anything that decides whether a lock may
    be **reclaimed** must use :func:`read_lock_state` instead — see F-002.
    """
    return read_lock_state(path)[1]


def malformed_lock_message(path: Path) -> str:
    """The fail-closed refusal for a lockfile that exists but cannot be read."""
    return (
        f"the drive lock {path} exists but cannot be read or parsed; refusing to "
        "reclaim a lock whose holder cannot be identified (FR-10.5, fail "
        "closed). Confirm no gauntlet process is driving this worktree "
        "(`gauntlet status`), then remove the file by hand."
    )


def record_is_live(rec: LockRecord) -> bool:
    """True unless the lock's holder is *proven* gone (FR-10.5).

    A drive lock must fail **closed**: reclaim only when we can prove the
    holder is dead (`os.kill` → ``ProcessLookupError``) or has been replaced
    by a different process (the recorded *and* a freshly-read identity are
    both present and differ → PID reuse). An ``os.kill``-live pid whose
    identity is *unverifiable* — recorded ``null`` at capture, or unreadable
    now (a transient ``ps`` failure, or an unsupported platform) — is treated
    as LIVE, so a possibly-running driver never has its lock stolen and two
    orchestrators can never drive one worktree (review F-001).

    This is the deliberate opposite of ``procident.process_is_alive``, which
    fails closed the *other* way for re-attach (FR-7.2: unverifiable → treat
    as orphaned → recover). For mutual exclusion, unverifiable must block.
    """
    try:
        os.kill(rec.pid, 0)
    except ProcessLookupError:
        return False  # proven dead → reclaimable as stale
    except PermissionError:
        pass  # exists (owned by another user) — the reuse check decides
    except OSError:
        return True  # cannot signal → do not assume gone; keep the lock
    recorded = ProcessIdentity.from_dict(rec.proc_identity)
    if recorded is None:
        return True  # alive, identity unverifiable → cannot prove reuse
    fresh = read_process_identity(rec.pid)
    if fresh is None:
        return True  # alive, fresh read failed → cannot prove reuse
    # Both identities present: equal → same live process (block); differ →
    # the pid was reused by a new process → the original is gone (reclaim).
    return recorded.same_process(fresh)


def link_into_place(lock_path: Path, nonce: str, payload: str) -> bool:
    """Atomically publish ``payload`` at ``lock_path`` iff it does not exist.

    Write the full content to a unique temp first, then ``os.link`` it into
    place — ``link`` fails if the target exists, so it is an atomic
    create-if-absent **and** the lock is *never* observed empty (unlike
    ``O_CREAT|O_EXCL`` then write, which leaves a zero-byte window a racing
    acquirer could misread as corrupt and reclaim). Returns ``True`` on win.
    """
    tmp = lock_path.with_name(f"{lock_path.name}.{nonce}.tmp")
    tmp.write_text(payload)
    try:
        os.link(tmp, lock_path)
        return True
    except FileExistsError:
        return False
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def unlink_if_nonce(lock_path: Path, nonce: str) -> bool:
    """Unlink ``lock_path`` only if it still carries ``nonce``; True if removed.

    The F-004 guard in one place: a holder that was already reclaimed-as-stale
    must never unlink the *new* owner's fresh lock.
    """
    current = read_record(lock_path)
    if current is None or current.nonce != nonce:
        return False
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass
    return True


__all__ = [
    "DRIVING_LOCK_NAME",
    "LOCK_ABSENT",
    "LOCK_ACQUIRE_RETRIES",
    "LOCK_MALFORMED",
    "LOCK_PRESENT",
    "LockRecord",
    "link_into_place",
    "malformed_lock_message",
    "new_record",
    "read_lock_state",
    "read_record",
    "record_is_live",
    "unlink_if_nonce",
    "utc_stamp",
]
