"""Run lifecycle: new / run / status / approve / reject / resume / abort / rollback.

Glue between the CLI and the :class:`Orchestrator`. Owns the on-disk layout
(FR-4.1), the entry contract (FR-10.1), branch management (FR-9.1), the
engine-managed judge lifecycle (FR-7.1), and guarded rollback (FR-9.9 /
review F-010).
"""

from __future__ import annotations

import atexit
import contextlib
import getpass
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import sys
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.engine import (
    gitops,
    journal as J,
    locking,
    manifest as M,
    prd_stub,
    recovery_exec as RX,
    worktree as WT,
)
from gauntlet.engine.recovery import AbortAction, NoProgressError
from gauntlet.engine.config import RESUME_ON_QUOTA_AUTO, RunConfig
from gauntlet.engine.execution import (
    RunPaths,
    StateDirNotContained,
    engine_bookkeeping_candidates,
    governed_artifact_paths,
    human_owned_excludes,
    run_bookkeeping_excludes,
    run_bookkeeping_paths,
)
from gauntlet.engine.identity import resolve_operator_identity
from gauntlet.engine.judgeproc import (
    JUDGE_RECORD_NAME,
    ManagedJudge,
    read_judge_record,
)
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.orchestrator import Orchestrator, ResponseAction
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.validate import validate_pipeline
from gauntlet.logging.redact import RedactingWriter, build_redactor
from gauntlet.procident import (
    ProcessIdentity,
    process_is_alive,
    read_process_identity,
)

# The drive-lockfile name (FR-10.5), identical at BOTH scopes P7b now writes:
# the worktree-global *tree guard* at `<run_root>/.driving.lock` (retained, and
# gitignored) and the per-run *driving lock* at
# `<run_root>/<slug>/<run-id>/.driving.lock`. See the design note above
# `_tree_lock_path` for why both exist while the tree is still shared.
DRIVING_LOCK_NAME = locking.DRIVING_LOCK_NAME

# The transient pre-signal recovery intent (operator-aids P4, FR-5.6 / §6.4).
# Lives in the run-instance dir; written durably *before* `recover` signals and
# unlinked only after the manifest recovery record is durably appended, so its
# presence on a later mutating invocation means "a verified kill began but the
# manifest was not finalized" — the signal crash reconciliation keys on.
RECOVERY_INTENT_NAME = ".recovery-intent.json"

# `recover` bounded SIGTERM→SIGKILL grace (FR-5.2), mirroring the timeout-kill
# path but TERM-first so a driver can flush. The poll interval bounds how often
# we re-check the group between the TERM and the escalation to KILL.
_RECOVER_SIGTERM_GRACE_S = 10.0
_RECOVER_POLL_INTERVAL_S = 0.1

# Console sidecar layout (also imported by web.jobproc so the two agree). The
# engine needs these to honour the run-id reservation handshake (FR-6.1a,
# review F-005): the console supervisor writes a single-use reservation token
# under `run_dir/.serve/` *before* launching this child, and `start()` accepts a
# pre-existing run dir only when it is exactly that fresh reservation.
SERVE_DIRNAME = ".serve"
RESERVATION_FILENAME = "reservation"

# Bounded retries for the acquire loop when racing a stale-lock reclaim, so a
# pathological churn raises rather than spins (fail closed). Shared with the
# repo-global lock via `engine.locking` (P7b).
_LOCK_ACQUIRE_RETRIES = locking.LOCK_ACQUIRE_RETRIES

# Marker written into a scaffolded PRD; the entry contract refuses to run while
# it is still present (FR-10.1 / review OQ-1: existence + non-stub-ness). The
# marker, the single committable stub template, the §6 manifest parser, and the
# fail-closed gate now all live in :mod:`gauntlet.engine.prd_stub` (P2); it is
# re-exported here so existing importers keep working.
PRD_STUB_MARKER = prd_stub.PRD_STUB_MARKER


class EntryContractError(RuntimeError):
    """The entry contract (FR-10.1) is not satisfied."""


class RollbackGuardError(RuntimeError):
    """A rollback guard (review F-010) refused the operation."""


class AbortGuardError(RuntimeError):
    """`abort()` refused because the target run is terminal (review F-002)."""


class RecoverError(RuntimeError):
    """Base for `gauntlet recover` outcomes that are not a successful recovery."""


class RecoverRefused(RecoverError):
    """`recover` refused fail-closed (FR-5.1/FR-5.4/FR-5.5): no signal was sent.

    The target could not be fully verified (absent/foreign/dead/recycled/
    regrouped lock, or an unobtainable datum), OR `recover` was invoked inside a
    pipeline-agent context (``GAUNTLET_STEP_ID`` set — the operator-only
    boundary). The message names the reason and the safe alternatives.
    """


class RecoverConcurrent(RecoverError):
    """`recover` aborted a race with a concurrently-finishing/relaunching driver.

    No signal was sent and the manifest was not mutated (FR-5.6 steps 2–3): the
    in-flight step transitioned out of ``running`` between capture and action, or
    the lock's ``nonce`` changed/vanished immediately before signalling.
    """


class RecoverSignalError(RecoverError):
    """`recover` could not deliver the kill to a verified-but-unsignalable driver.

    The target's identity was proven but the OS refused the signal (``EPERM`` —
    e.g. the process changed credentials). The driver is **still alive** and was
    NOT terminated, so the step is never marked ``INTERRUPTED``; the caller clears
    the durable intent so reconciliation never retries the un-killable signal
    forever (review F-005). The message names manual termination as the safe path.
    """


class UnsafeRunSegment(ValueError):
    """A slug or run-id that is not a single, traversal-free path segment.

    The write/control path's first line of FR-10.1 containment, mirroring the
    read model's ``web.store._safe_segment`` (review F-001): a slug or
    ``--run-id`` flows straight into filesystem paths, so anything containing a
    path separator, ``.``/``..``, NUL, or that is empty is refused before any
    path is built.
    """


def safe_run_segment(seg: str, *, kind: str) -> str:
    """Reject a slug/run-id that could escape the run root (FR-10.1, F-001)."""
    if not seg or seg in (".", "..") or "/" in seg or "\\" in seg or "\x00" in seg:
        raise UnsafeRunSegment(f"unsafe {kind} segment: {seg!r}")
    return seg


def _reservation_matches(run_dir: Path, token: str | None) -> bool:
    """True iff ``run_dir`` holds exactly the fresh reservation for ``token``.

    The supervisor writes the single-use token under ``.serve/`` before launch
    (FR-6.1a); this lets a child engine verify, race-free, that a pre-existing
    run dir is its own fresh reservation rather than a prior run's leftover
    diagnostic state, which must never be reused/overwritten (review F-005).
    """
    if not token:
        return False
    try:
        existing = (run_dir / SERVE_DIRNAME / RESERVATION_FILENAME).read_text().strip()
    except (OSError, ValueError):
        return False
    return bool(existing) and hmac.compare_digest(existing, token)


class ActiveRunError(RuntimeError):
    """`start()` refused because a non-terminal run is already active."""


class WorktreeUnavailableError(RuntimeError):
    """A run's dedicated worktree could not be made ready (P7c, spike §13).

    Distinct from :class:`WorktreeLockError`, which is about *who is driving*.
    This is about *which tree they would drive in*, and it is always raised
    without having mutated anything: the run is left exactly as it was, and the
    message carries the operator-chosen fallback (`resume --same-tree`).
    """


class WorktreeLockError(RuntimeError):
    """A driving verb refused: the worktree is already being driven (FR-10.5).

    The repo/worktree-scoped active-run lock is held by a **live** process
    driving some run (the same slug or a different one) against this worktree.
    Failing closed here is what makes "two orchestrators against one worktree"
    (R1) impossible by construction, not by UI heuristic.
    """


class StaleRunBranchError(RuntimeError):
    """`start()` refused: the run branch exists with commits not in its base.

    The branch is unmerged or divergent (e.g. a stale branch left at an older
    base, the case that silently rewound a worktree before this guard). Failing
    closed here is what makes "forgot to clean up" safe — the run never adopts a
    branch it cannot prove is spent.
    """


class RunBranchNotMergedError(RuntimeError):
    """`clean()` refused: the run branch is not fully merged into its base."""


class WorktreeDirtyError(RuntimeError):
    """A branch-switching op refused because the worktree has uncommitted work.

    Switching off the run branch with a dirty tree would carry the changes onto
    the base (or fail mid-checkout on conflict) — fail closed instead (F-2).
    """


class RunBranchStateError(RuntimeError):
    """`resume()` refused: the run branch is missing or disagrees with the manifest.

    Resume must continue the SAME branch the run committed to. Recreating it
    from base (the old behaviour) would silently drop the manifest's recorded
    commits and resume a divergent branch — fail closed instead (F-1).
    """


class BaseBranchError(RuntimeError):
    """`start()` refused: the resolved base is a machine-owned run branch (F-3).

    `base_branch: current` while sitting on a `gauntlet/*` branch would record a
    run branch as the base, which later wedges `finish` (branch == base). The
    base must be an integration branch, never under ``branch_prefix``.
    """


class FinishError(RuntimeError):
    """`finish()` refused (run not done, dirty tree, or a merge conflict)."""


class _MigrationStepFailed(RuntimeError):
    """Internal: a migration step failed after the worktree was created.

    Never escapes :meth:`RunManager._migrate_locked`. It carries only the CAUSE;
    the operator-facing message is composed after the unwind has run and been
    verified, so no refusal can claim a post-state that was not observed
    (review F-004).
    """


class MigrateWorktreeRefused(RuntimeError):
    """`migrate-worktree` refused, having mutated nothing (spike §10).

    Every row of the §10 refusal matrix raises this, and every one of them
    leaves the run **fully resumable in `same_tree` mode** with the blocker
    named. That clause is the R1 obligation and the whole point of the design:
    migration is an *added capability*, so a run must never be wedged by the
    migration being impossible. Any message raised from here that does not tell
    the operator what they can still do is a bug in this class's contract, not
    a stylistic omission.
    """


# `base_branch: current` (case-insensitive) means "branch from whatever branch
# is checked out now" — so a run stacks on the integration branch you are on
# without a per-run flag. The resolved name is recorded in the manifest.
_BASE_CURRENT_SENTINELS = frozenset({"current", "@current"})


# A run in one of these states is finished and may be superseded by a fresh
# `start()`. Any other state (running / parked) is still live — starting over it
# would orphan it and risk competing agents against one worktree.
_TERMINAL_RUN_STATES = frozenset({M.RUN_DONE, M.RUN_ABORTED, M.RUN_FAILED})


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


# Poll cadence for the in-process auto-resume wait (FR-3.4): the wait re-checks
# the wall clock at least this often so a host suspension during the wait shortens
# it correctly (the heartbeat/wall clock jumped forward) instead of over-sleeping.
_AUTO_RESUME_POLL_S = 60.0

# Auto-resume decisions (return of :func:`next_auto_resume_action`).
AUTO_RESUME_NONE = "none"
AUTO_RESUME_WAIT = "wait"
AUTO_RESUME_RESUME = "resume"
AUTO_RESUME_EXHAUST = "exhaust"


def next_auto_resume_action(
    scheduled_resume: "M.ScheduledResume | None", now: datetime
) -> tuple[str, float]:
    """Decide the next auto-resume step for a scheduled usage-limit park (FR-3.4).

    Pure so the reconciliation logic is unit-testable with a stubbed clock:

    * ``none`` — nothing scheduled.
    * ``exhaust`` — attempts hit the ceiling; fall back to a plain park.
    * ``resume`` — the reset time has passed (or is unparseable → resume now);
      the driver should perform the continuation resume.
    * ``wait`` — the reset time is still ahead; the second element is the seconds
      to wait (the caller re-checks after, staying suspend-aware).
    """
    if scheduled_resume is None:
        return (AUTO_RESUME_NONE, 0.0)
    if scheduled_resume.attempts >= scheduled_resume.max_attempts:
        return (AUTO_RESUME_EXHAUST, 0.0)
    try:
        target = datetime.fromisoformat(scheduled_resume.attempt_at)
    except (ValueError, TypeError):
        return (AUTO_RESUME_RESUME, 0.0)  # unparseable → resume now (fail toward action)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    remaining = (target - now).total_seconds()
    if remaining <= 0:
        return (AUTO_RESUME_RESUME, 0.0)
    return (AUTO_RESUME_WAIT, remaining)


# The lock record moved to `engine.locking` in P7b so the three lock layers
# (tree guard, per-run driving lock, repo-global git lock) share ONE reclaim
# rule rather than three copies that can drift. The historical name is kept
# because `engine.operator` — and the tests — import it from here.
_LockRecord = locking.LockRecord


@dataclass
class _LockHandle:
    """One acquisition of the drive lock; the nonce authorises the release.

    P7b publishes a single record — one nonce — at up to two paths:

    * ``path`` — the worktree-global **tree guard** at
      ``<run_root>/.driving.lock``. Always present.
    * ``run_path`` — the per-run **driving lock** at
      ``<run_root>/<slug>/<run-id>/.driving.lock``. ``None`` until the run dir
      exists (``start`` attaches it once the dir is minted; every other verb
      takes both up front).

    Both files carry the same nonce, so a nonce-keyed release — the F-004 guard,
    and `recover`'s step-8 release of a *wedged driver's* lock via the intent's
    ``lock_nonce`` — identifies the pair unambiguously.
    """

    path: Path
    nonce: str
    run_path: Path | None = None


@dataclass
class _RecoveryIntent:
    """The transient ``.recovery-intent.json`` content (FR-5.6 / §6.4).

    The durable pre-signal companion to the §6.4 recovery record: it freezes the
    FR-5.1-verified identity datums (so a crash-reconciled finalize trusts them
    instead of re-running a liveness gate against a now-dead PID) plus the prior
    states needed to compose the record and the operator ``reason``. ``step_id``
    is the *rendered* step id (``<id>`` / ``<id>.<iteration>``), matched back to a
    record by re-rendering — never by parsing — so a dotted id is unambiguous.
    """

    ts: str
    actor: str
    actor_source: str
    reason: str | None
    lock_nonce: str
    pid: int
    pgid: int
    proc_identity: dict | None
    host: str
    step_id: str
    prior_step_status: str
    prior_run_status: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "actor": self.actor,
                "actor_source": self.actor_source,
                "reason": self.reason,
                "lock_nonce": self.lock_nonce,
                "pid": self.pid,
                "pgid": self.pgid,
                "proc_identity": self.proc_identity,
                "host": self.host,
                "step_id": self.step_id,
                "prior_step_status": self.prior_step_status,
                "prior_run_status": self.prior_run_status,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "_RecoveryIntent | None":
        """Parse the intent, or ``None`` if malformed/incomplete (fail closed).

        A malformed intent carries no trustworthy facts, so reconciliation must
        not signal or finalize from it — it stays untouched for the operator.
        """
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            lock_nonce = data["lock_nonce"]
            step_id = data["step_id"]
            host = data.get("host", "")
            actor = data.get("actor", "")
            actor_source = data.get("actor_source", "")
            ts = data.get("ts", "")
            reason = data.get("reason")
            proc_identity = data.get("proc_identity")
            prior_step_status = data["prior_step_status"]
            prior_run_status = data["prior_run_status"]
            if not all(
                isinstance(v, str) and v
                for v in (lock_nonce, step_id, prior_step_status, prior_run_status)
            ):
                return None
            if not all(isinstance(v, str) for v in (host, actor, actor_source, ts)):
                return None
            if reason is not None and not isinstance(reason, str):
                return None
            if proc_identity is not None and not isinstance(proc_identity, dict):
                return None
            return cls(
                ts=ts,
                actor=actor,
                actor_source=actor_source,
                reason=reason,
                lock_nonce=lock_nonce,
                pid=int(data["pid"]),
                pgid=int(data.get("pgid", data["pid"])),
                proc_identity=proc_identity,
                host=host,
                step_id=step_id,
                prior_step_status=prior_step_status,
                prior_run_status=prior_run_status,
            )
        except (ValueError, KeyError, TypeError):
            return None


def _fsync_dir(path: Path) -> None:
    """``fsync`` a directory so a rename/unlink within it survives power loss.

    Best-effort: some platforms refuse a directory ``fsync`` (``EINVAL``/
    ``EISDIR``) — the atomic ``rename`` is still crash-consistent there, only the
    extra power-loss durability is unavailable, so a failure is swallowed.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_durable(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` durably and atomically (FR-5.6).

    temp → write → ``flush`` → ``fsync`` → ``rename`` → ``fsync`` the containing
    dir, so a crash or power loss leaves either no file or the complete one —
    atomic ``rename`` alone is not durable across power loss.
    """
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _unlink_durable(path: Path) -> None:
    """Unlink ``path`` and ``fsync`` its dir so the deletion itself is durable."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    _fsync_dir(path.parent)


def _pid_is_live(pid: int) -> bool:
    """``os.kill(pid, 0)`` liveness probe; fail-closed (unknown errors → live).

    ``ProcessLookupError`` is the only *proof* of absence; a permission error
    means the pid exists (owned by another user); any other ``OSError`` cannot
    prove it gone, so it is treated as live (the identity check decides).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    """True unless the process group is *proven* empty (``ProcessLookupError``)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _signal_process_group(
    pgid: int, *, grace_s: float = _RECOVER_SIGTERM_GRACE_S
) -> str:
    """SIGTERM the group, wait a bounded grace, then SIGKILL if still alive (FR-5.2).

    Mirrors the timeout-kill path (``adapters/process.py``) but TERM-first so a
    driver gets a chance to flush. Returns the §6.4 ``signal_outcome``:
    ``terminated_sigterm`` (gone within the grace), ``terminated_sigkill`` (only
    after the escalation), or ``already_dead`` (the group was gone before TERM).
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return M.SIGNAL_ALREADY_DEAD
    except PermissionError:
        # Owned by another user — we proved identity, so escalate to KILL below.
        pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return M.SIGNAL_TERMINATED_SIGTERM
        time.sleep(_RECOVER_POLL_INTERVAL_S)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return M.SIGNAL_TERMINATED_SIGTERM  # exited right at the boundary
    except PermissionError as exc:
        # Identity was proven (it IS our driver) yet the OS refuses the signal —
        # the process is still alive and CANNOT be terminated by us. Marking a
        # live driver INTERRUPTED would be a lie, so fail closed with explicit
        # guidance; the caller clears the durable intent so reconciliation does
        # not retry this un-killable signal on every later entry point (F-005).
        raise RecoverSignalError(
            f"refusing to finalize recovery: permission denied signalling the "
            f"verified driver's process group {pgid} ({exc}). The driver is "
            "STILL ALIVE and was not terminated; no step was marked interrupted. "
            f"Terminate it manually with sufficient privileges (e.g. `sudo kill "
            f"-KILL -{pgid}`), then run `gauntlet resume`."
        ) from exc
    return M.SIGNAL_TERMINATED_SIGKILL


def _path_within(child: Path, ancestor: Path) -> bool:
    """True iff ``child`` (already resolved) is at or under ``ancestor`` (resolved).

    The write/control-path mirror of ``operator._within`` (FR-10.1 / PRD §7
    containment): a path built from external bytes (the active-run pointer, a
    ``.recovery-intent.json`` symlink) must be proven inside the run tree before
    any read or write drives signalling or a manifest mutation.
    """
    try:
        child.relative_to(ancestor)
        return True
    except ValueError:
        return False


@dataclass
class RunLayout:
    repo_root: Path
    config: RunConfig
    slug: str

    @property
    def slug_dir(self) -> Path:
        return self.repo_root / self.config.run_root / self.slug

    @property
    def prd_path(self) -> Path:
        return self.slug_dir / "prd.md"

    @property
    def active_pointer(self) -> Path:
        return self.slug_dir / "active-run.txt"

    def run_dir(self, name: str) -> Path:
        return self.slug_dir / name

    def active_run_dir(self) -> Path:
        if not self.active_pointer.exists():
            raise FileNotFoundError(
                f"no active run for {self.slug!r}; has `gauntlet run` been started?"
            )
        run_id = self.active_pointer.read_text().strip()
        # Containment (FR-10.1 / PRD §7): the pointer's bytes flow straight into a
        # path that the mutating verbs (recover/resume) then read AND write —
        # manifest, recovery intent, lock. An unvalidated segment could carry a
        # traversal or absolute path that redirects those mutations outside the run
        # tree. Reject an unsafe segment, then require the resolved dir to remain
        # beneath the slug dir (catching a run-dir symlink that escapes), before
        # any caller reads or writes through it (review F-003).
        safe_run_segment(run_id, kind="run-id")
        candidate = self.slug_dir / run_id
        try:
            if not _path_within(candidate.resolve(), self.slug_dir.resolve()):
                raise UnsafeRunSegment(
                    f"active run dir for {self.slug!r} escapes the slug tree: "
                    f"{run_id!r}"
                )
        except (OSError, RuntimeError) as exc:
            raise UnsafeRunSegment(
                f"active run dir for {self.slug!r} is unresolvable ({exc}): "
                f"{run_id!r}"
            ) from exc
        return candidate


class RunManager:
    def __init__(self, repo_root: Path, config: RunConfig | None = None) -> None:
        self.repo_root = repo_root
        self.config = config or RunConfig.load(repo_root / ".gauntlet/config.yaml")
        # The configured redaction list (FR-4.4) governs every byte the run
        # writes; default-on even with an empty `redaction:` section.
        self.writer = RedactingWriter(build_redactor(self.config.redaction))
        # The worktree lock this manager currently holds, if any (FR-10.5). Kept
        # in memory so an atexit fallback can release it on an unclean exit that
        # bypasses the per-verb `finally`.
        self._held_lock: _LockHandle | None = None
        # The ACTIVE run's roots (P7c, problem B). `None` outside a driving
        # verb, which is the same-tree answer every read-only surface wants.
        # Set by `_run_paths` for the duration of one drive and never longer:
        # `work_root` is a property OF A RUN, not of a manager, and a manager
        # that remembered one run's tree past its verb would hand it to the
        # next — the exact class of defect the carrier exists to prevent.
        self._paths: RunPaths | None = None

    @property
    def operator_root(self) -> Path:
        """The operator's own checkout, named deliberately (P7a, spike §9.4).

        A handful of verbs act on the human's tree ON PURPOSE and must keep
        doing so after P7c: ``finish`` merges the run branch into the base
        *there*, ``clean`` deletes the branch from *there*, `base_branch:
        current` resolves against the branch the human is standing on, and the
        governed proposal apply patches assets *there*. Those are the opposite
        of the sites P7 is fixing, so they get an explicit name rather than the
        ambiguous ``repo_root`` — ``tests/unit/test_root_scope.py`` bans the
        ambiguous one from every work-scoped call, which makes this list
        greppable and auditable instead of implicit.
        """
        return self.repo_root

    @property
    def work_root(self) -> Path:
        """The tree a run's agents edit and the engine commits in.

        Per-RUN, not per-manager (P7c): inside a driving verb that resolved a
        `dedicated` run's paths this is that run's own worktree; everywhere
        else — every `same_tree` run, and every read-only surface — it is the
        operator's checkout, exactly as before.
        """
        return self._paths.work_root if self._paths is not None else self.repo_root

    @property
    def paths(self) -> RunPaths | None:
        """The active run's roots, or ``None`` outside a driving verb."""
        return self._paths

    @property
    def configured_worktree_mode(self) -> str:
        """`config.worktree.mode` — what a NEW run is born as (spike §13).

        Read in exactly ONE place: :meth:`start`. Every other caller asks
        :meth:`_effective_worktree_mode`, which resolves from evidence and from
        what the run recorded at birth. That asymmetry is not stylistic — see
        that method for why config must never decide an existing run's mode.
        """
        cfg = getattr(self.config, "worktree", None)
        return getattr(cfg, "mode", None) or WT.MODE_SAME_TREE

    def _effective_worktree_mode(self, man: Manifest) -> str:
        """The mode THIS run actually drives in — evidence first, config never.

        The safety boundary between P7c-1 and P7c-2 (`proposals/
        P7c-split-seam.md` §3). Resolution order, and each rule's reason:

        1. **a registered worktree for ``man.branch``** → `dedicated`. The tree
           is observable ground truth, and ``worktree list --porcelain`` answers
           with a dead driver, which is what makes it usable from a recovery
           assessment (spike §10).
        2. **an unreleased ``WorktreeAdopted`` in the journal** → `dedicated`.
           The tree is *missing* but was adopted: spike §11 row 2. The answer
           must be "dedicated, recreate it", never "same_tree" — resolving this
           case to `same_tree` would silently drop the run back into the
           operator's checkout at exactly the moment its own tree vanished.
        3. **``man.worktree_mode``** → what the run was born as.
        4. otherwise → `same_tree`: a pre-P7c run, the legacy population (§16).

        **``config.worktree.mode`` is deliberately absent from this list.** If
        it appeared here, an operator setting `dedicated` on a repository that
        already has runs would move every one of them into a worktree on its
        next `resume` — no operator action, no journal event, at a moment they
        believe they only changed a default. That is the auto-migration spike
        §10 forbids ("a pre-P7 run is never auto-migrated, and never wedged"),
        and it would arrive in the commit that never mentions migration.

        Rules 1 and 2 are §10's detection rule in both directions, so P7c-2's
        migration eligibility predicate is the negation of this one and cannot
        drift from it.
        """
        try:
            # Scoped to the engine's OWN worktrees root: the operator's main
            # checkout is itself a registered worktree, and in `same_tree` mode
            # it is exactly where this branch is checked out. Unscoped, every
            # same_tree run would resolve `dedicated`.
            if WT.observe(
                self.operator_root, man.branch,
                main_root=self._main_worktree_root(),
            ) is not None:
                return WT.MODE_DEDICATED
        except gitops.GitError:
            # Fail closed toward the run's OWN record rather than guessing:
            # an unreadable worktree list must not be read as "no worktree".
            pass
        if self._journal_says_adopted(man):
            return WT.MODE_DEDICATED
        recorded = man.worktree_mode
        if recorded is None:
            return WT.MODE_SAME_TREE  # rule 4: a pre-P7c run
        if recorded not in WT.MODES:
            # F-006, fail closed. `Manifest.worktree_mode` is a plain string on
            # disk, so a corrupt manifest — or one written by a FORWARD version
            # that knows a third mode — can carry a value this engine does not
            # understand. Returning it verbatim was silently safe-looking and
            # actively dangerous: `_run_paths` treats anything that is not
            # exactly `dedicated` as same-tree, so an unrecognized value made
            # resume check out and mutate the OPERATOR's tree. "I do not know
            # which tree this run drives" must stop the verb, not pick one.
            raise WorktreeUnavailableError(
                f"run {man.run_id!r} records worktree mode {recorded!r}, which "
                f"this engine does not recognize (known: {sorted(WT.MODES)}). "
                "Refusing to guess which tree it drives — guessing wrong means "
                "driving the operator's checkout. If the manifest is corrupt, "
                "restore it from the journal (`gauntlet status` names the "
                "projection state); if it was written by a newer Gauntlet, use "
                "that version."
            )
        return recorded

    # The run statuses that end a run. Spike §10's first table row: "completed /
    # aborted / failed → rendered as `worktree: null, mode: same_tree`; never
    # migrated", because a terminal run has no live tree to isolate. Refusing
    # them costs nothing — a terminal run is not driving, so it has nothing to
    # gain from its own tree, and a `failed` run stays as resumable in
    # `same_tree` after the refusal as it was before it.
    TERMINAL_RUN_STATUSES = (M.RUN_DONE, M.RUN_ABORTED, M.RUN_FAILED)

    @staticmethod
    def _migratable_liveness() -> tuple[str, ...]:
        """The driver states under which a run's tree may be moved or removed.

        §10: "the P7 engine refuses to migrate a run whose lock is `alive` or
        `indeterminate`", per ``_lock_is_live``'s deliberate asymmetry.
        ``orphaned`` is *proven* dead and ``none`` is *no driver at all*;
        everything else fails closed. The values come from
        :mod:`gauntlet.engine.operator` rather than being re-literal'd, so this
        gate and the FR-2.4 table it reads from cannot drift apart — the same
        reason ``_reap_orphaned_judge`` imports them (§6.4: liveness has one
        sanctioned primitive).
        """
        from gauntlet.engine import operator

        return (operator.LIVENESS_ORPHANED, operator.LIVENESS_NONE)

    def migration_blocker(self, man: Manifest, *, liveness: str) -> str | None:
        """Why this run may NOT be migrated to a dedicated worktree, or ``None``.

        **The eligibility rule is the NEGATION of
        :meth:`_effective_worktree_mode`, not a second rule.** A run is
        migratable iff that method resolves ``same_tree`` — which is exactly
        §10's detection rule ("a run is `same_tree` iff its journal carries no
        `WorktreeAdopted` event *and* `git worktree list --porcelain` registers
        no worktree for `man.branch`") read in the other direction — **and** it
        is non-terminal **and** its driver is provably dead or absent.

        Deriving the tree half independently is how the two rules drift, and
        drift here is not cosmetic: an eligibility rule that says `same_tree`
        where the resolver says `dedicated` would move a run that already has a
        tree, and the converse would refuse a run that should move. So this
        method calls the resolver and reads no worktree evidence of its own —
        `tests/unit/test_worktree_migrate_p7c.py` asserts both the agreement
        (behaviourally, over every evidence shape) and the derivation
        (statically, so a future edit cannot re-introduce a second reader).

        Returns a complete operator-facing sentence, not a code: every caller
        renders it verbatim, and each one ends with what the run can still do.
        Raises :class:`WorktreeUnavailableError` for a manifest whose recorded
        mode this engine cannot recognize — "I do not know which tree this run
        drives" must stop the verb rather than pick one (F-006).
        """
        mode = self._effective_worktree_mode(man)
        if mode != WT.MODE_SAME_TREE:
            return (
                f"run {man.run_id!r} already drives a dedicated worktree "
                f"(effective mode {mode!r}); there is nothing to migrate. "
                f"`gauntlet status {man.slug}` shows the tree."
            )
        if man.status in self.TERMINAL_RUN_STATUSES:
            return (
                f"run {man.run_id!r} is {man.status!r} — a terminal run is "
                "never migrated (spike §10): it is not driving, so it has no "
                "work to isolate in a tree of its own. Its evidence stays "
                "exactly where it is."
            )
        if liveness not in self._migratable_liveness():
            from gauntlet.engine import operator

            why = {
                operator.LIVENESS_ALIVE: (
                    "a driver is LIVE and is driving this run in your checkout "
                    "right now; moving the tree under it would pull the ground "
                    "out from a running agent"
                ),
                operator.LIVENESS_INDETERMINATE: (
                    "the driver's liveness cannot be PROVEN either way "
                    "(indeterminate) — an unverifiable process is treated as "
                    "live, never as gone (fail closed)"
                ),
            }.get(liveness, f"driver liveness is {liveness!r}")
            return (
                f"refusing to migrate {man.slug!r}: {why}. Wait for the driver "
                f"to finish or park, or inspect with `gauntlet status "
                f"{man.slug}` / `gauntlet logs {man.slug}`."
            )
        return self._migration_precondition_blocker(man)

    def _migration_precondition_blocker(self, man: Manifest) -> str | None:
        """The git preconditions §10 requires, or ``None``. Not a mode rule.

        Spike §10's last table row names four cannot-migrate cases, and two of
        them are properties of the operator's checkout rather than of the run's
        state: **"dirty operator tree"** and **"branch checked out elsewhere"**.
        Deliberately separate from :meth:`migration_blocker`'s state legs, which
        answer "what mode is this run in?" from one authority — this answers
        "would the git operation succeed right now?", a different question with a
        different source. The static test keeps both honest: neither may
        re-derive the MODE.

        Both preconditions are consulted by the verb AND by the `status` offer,
        which is what makes the R4 claim true rather than aspirational (review
        F-006): before this, the normal parked `same_tree` state — run branch
        checked out in the operator's tree — produced an `executable: true`
        offer that git was certain to refuse.

        * **branch held elsewhere** (F-006): git refuses a second worktree for a
          checked-out branch (E2-A). This verb will not check out or move a
          branch in the operator's tree, so the operator must step off first.
        * **dirty operator tree** (F-005): a same_tree run's uncommitted work
          lives in the operator's checkout. Migration builds a clean tree from
          the committed branch tip, so that work would be STRANDED — silently,
          and on whatever branch the operator has since moved to (git carries
          compatible edits across a checkout). Engine bookkeeping and the
          governed artifacts are excluded: the first is invisible everywhere
          else, and the second is republished into the run tree by the sync, so
          neither is stranded.
        """
        try:
            entry = gitops.worktree_for_branch(self.operator_root, man.branch)
        except gitops.GitError as exc:
            return (
                f"could not read git's worktree list ({exc}), so this cannot "
                "prove the run branch is free to be checked out in a new "
                "worktree."
            )
        # An ENGINE-owned tree does not block: for the new root because the run
        # already has its tree, and for the pre-P7e root (P7e) because
        # relocating exactly that tree is what `migrate-worktree` now also does.
        # Refusing it here would make the relocation unreachable and leave the
        # legacy run with no action at all — spike §10's "never wedged".
        engine_owned = False
        if entry is not None:
            try:
                engine_owned = WT.is_inside_worktrees_root(
                    entry.path, self._main_worktree_root()
                ) or WT.legacy_observe(
                    self.operator_root, man.branch,
                    common_dir=self._git_common_dir(),
                ) is not None
            except (gitops.GitError, OSError):
                engine_owned = False
        if entry is not None and not engine_owned:
            return (
                f"branch {man.branch!r} is currently checked out at "
                f"{entry.path}, and git refuses a second worktree for a "
                "checked-out branch (spike E2-A). This verb will not check out "
                "or move a branch in your tree — that is the invariant P7 "
                f"exists to protect — so step off it first: `git -C "
                f"{entry.path} checkout {man.base_branch}` (or any branch that "
                f"is not {man.branch!r}), then migrate."
            )
        return self._dirty_operator_tree_blocker(man)

    def _dirty_operator_tree_blocker(self, man: Manifest) -> str | None:
        """Spike §10's "dirty operator tree" cannot-migrate case (review F-005)."""
        layout = self.layout(man.slug)
        try:
            run_dir = layout.run_dir(man.run_id)
        except (UnsafeRunSegment, OSError):
            return None
        try:
            excludes = run_bookkeeping_excludes(
                self.operator_root, run_dir, layout.slug_dir
            ) + governed_artifact_paths(self.operator_root, layout.slug_dir)
            dirt = gitops.status_porcelain(
                self.operator_root, exclude=excludes, untracked_all=True
            )
        except (gitops.GitError, StateDirNotContained, OSError):
            return None  # cannot observe → the git operation itself fails closed
        if not dirt:
            return None
        listing = "\n  ".join(dirt.splitlines()[:8])
        return (
            f"your checkout has uncommitted work that this run may own:\n"
            f"  Tree inspected: {self.operator_root}\n  {listing}\n"
            "A `same_tree` run's work-in-progress lives in YOUR checkout. "
            "Migration builds the run's new tree from the committed branch tip, "
            "so anything uncommitted would be left behind here — on whatever "
            "branch you are standing on — while the agents carried on somewhere "
            "else (spike §10, 'dirty operator tree'). Commit it to the run "
            "branch or stash it, then migrate."
        )

    def _migration_rollback_blocker(
        self, man: Manifest, *, liveness: str
    ) -> str | None:
        """Why a migration may NOT be rolled back, or ``None``. Same derivation.

        The mirror of :meth:`migration_blocker`, and it asks the same resolver:
        a migration exists to be rolled back iff the run resolves ``dedicated``
        **from evidence** while its manifest does *not* record it as born
        dedicated. That second clause is what makes rollback exact rather than
        approximate — migration deliberately writes no ``worktree_mode`` (see
        :meth:`migrate_worktree`), so removing the tree and journalling the
        release returns the run to ``same_tree`` by the resolver's own rules 3
        and 4. A run BORN dedicated has no migration to undo: rule 3 would keep
        resolving ``dedicated`` and the next drive would simply rebuild the
        tree, so telling the operator "rolled back" would be a lie.
        """
        mode = self._effective_worktree_mode(man)
        if mode != WT.MODE_DEDICATED:
            return (
                f"run {man.run_id!r} drives in {mode!r} mode; there is no "
                "worktree migration to roll back."
            )
        if man.worktree_mode == WT.MODE_DEDICATED:
            return (
                f"run {man.run_id!r} was BORN dedicated (its manifest records "
                "`worktree_mode: dedicated`), so it was never migrated and "
                "there is nothing to roll back: removing the tree would only "
                "make the next drive rebuild it. End the run instead "
                f"(`gauntlet abort {man.slug}`, then `gauntlet clean "
                f"{man.slug}`)."
            )
        if man.status in self.TERMINAL_RUN_STATUSES:
            return (
                f"run {man.run_id!r} is {man.status!r}; a terminal run's tree "
                f"is removed by `gauntlet clean {man.slug}` (or `gauntlet "
                f"finish {man.slug}`), which also tidies the branch and the "
                "active-run pointer. Rollback is for a run that is still going."
            )
        if liveness not in self._migratable_liveness():
            return (
                f"refusing to roll back {man.slug!r}: driver liveness is "
                f"{liveness!r}, not provably gone — removing the run worktree "
                "under a driver that may be inside it is exactly what fails "
                f"closed here. Inspect with `gauntlet status {man.slug}`."
            )
        return None

    def _journal_says_adopted(self, man: Manifest) -> bool:
        """True when the journal records an adoption with no later release.

        Rule 2 of :meth:`_effective_worktree_mode`. Read-only and tolerant: the
        journal is authoritative for run state, but the worktree's existence is
        independently observable (rule 1), so an unreadable journal degrades to
        "no adoption recorded" rather than failing the verb.
        """
        try:
            run_dir = self.layout(man.slug).run_dir(man.run_id)
            events = J.read_events(run_dir)
        except (OSError, ValueError, J.JournalError):
            return False
        adopted = False
        for evt in events:
            kind = evt.get("kind")
            if kind == "WorktreeAdopted":
                adopted = True
            elif kind == "WorktreeReleased":
                adopted = False
        return adopted

    def _git_common_dir(self) -> Path:
        """The shared git dir, resolved from the OPERATOR's checkout.

        Deliberately not from the work root: the incident that most needs this
        path is acceptance A3 — recreating a run worktree that is MISSING — and
        running git inside the very tree that no longer exists would fail
        exactly then. The operator's checkout is the surviving root by
        construction, because the CLI was invoked from it (P7a/F-001).
        """
        return gitops.git_common_dir(self.operator_root)

    def _main_worktree_root(self) -> Path:
        """The repository's MAIN worktree — what run-worktree paths derive from.

        P7e's anchor, replacing :meth:`_git_common_dir` everywhere the question
        is "where does the engine put run worktrees?" (the common dir is still
        the answer to "where is shared git state?", which is why both exist).

        Resolved from the OPERATOR's checkout for the same reason
        :meth:`_git_common_dir` is: the incident that most needs this path is
        acceptance A3 — recreating a MISSING run worktree — and running git
        inside the tree that no longer exists would fail exactly then.

        Not ``self.operator_root`` itself: an operator may legitimately drive
        from their own linked worktree (spike §18.2 addition 5 even suggests
        making one), and the derived root must be the same from every vantage
        point or two checkouts of one repo would grow two sets of run
        worktrees, invisible to each other's containment checks.
        """
        return gitops.main_worktree_root(self.operator_root)

    @contextmanager
    def _run_paths(
        self,
        layout: "RunLayout",
        run_dir: Path,
        man: Manifest | None = None,
        *,
        slug: str,
        run_id: str,
        branch: str,
        mode: str,
        same_tree: bool = False,
    ):
        """Resolve this run's roots for one verb, ensuring the tree exists.

        The single place a run's `work_root` is decided, and the mechanism the
        P7b reviewer's F-001 was deferred here for. Everything downstream —
        the Orchestrator, every StepContext, the RecoveryExecutor, the
        verifier's disposable-copy parent and the judge's path boundary — takes
        its roots from the :class:`RunPaths` this yields, so the three roots can
        never be independently (mis)chosen per call site again.

        ``mode`` is the run's EFFECTIVE mode — from
        :meth:`_effective_worktree_mode` for an existing run, or from
        :attr:`configured_worktree_mode` for one being born in :meth:`start`.
        It is a required argument rather than something resolved here on
        purpose: the resolution rule is the anti-auto-migration boundary, and
        making every caller name the mode it resolved keeps that decision
        greppable instead of buried in a context manager.

        ``same_tree=True`` is the operator-chosen `--same-tree` fallback (spike
        §13). It is never automatic: if the worktree cannot be created, locked
        or verified we raise :class:`WT.WorktreeUnavailable` and the caller
        parks with that reason. Falling back silently would mutate the
        operator's checkout precisely when the machine is already in an
        unexpected state, which is the one thing P7 exists to prevent.

        Restores the previous value on exit rather than clearing to ``None``,
        so a nested verb (auto-resume inside a drive) cannot strand the outer
        one on the wrong tree.
        """
        prior = self._paths
        dedicated = mode == WT.MODE_DEDICATED and not same_tree
        work_root = self.repo_root
        if same_tree and mode == WT.MODE_DEDICATED:
            # F-010: skipping `ensure` is not enough to make the fallback
            # EXECUTABLE. If a registered worktree still holds the run branch,
            # the same-tree drive's `checkout_branch` in the operator's tree
            # hard-refuses (E2-B) — and the headline case reaches exactly that
            # state, because the submodule park raises AFTER `ensure` has
            # created and locked the tree. Offering an action that cannot run
            # is worse than offering none.
            #
            # So release the engine's OWN tree first, through the guarded path:
            # dirty work is snapshotted, never force-discarded (R2). A tree we
            # do not own is left alone and refused loudly — `--same-tree` is a
            # fallback, not an override.
            self._release_for_same_tree_fallback(run_dir, man, slug=slug, branch=branch)
        if dedicated:
            main_root = self._main_worktree_root()
            # P7e: passed only so `ensure` can recognise a tree still at the
            # pre-P7e root and refuse with the relocation command rather than
            # with `--same-tree`, which would drive in the operator's checkout.
            common = self._git_common_dir()
            # F-008: when the tree is registered-but-absent (spike §11 row 2)
            # this is a RECREATE, not a fresh create, and acceptance A3 says the
            # reconstruction must be verified — "the tree came back" and "the
            # tree came back CORRECT" are different claims. `WT.recreate` is the
            # only path that checks the recreated HEAD, so the missing-tree case
            # must route through it with the branch SHA the run's own state
            # recorded. Previously nothing in production called it, so the
            # advertised check never ran.
            expect = None
            state = WT.describe(self.operator_root, mode=mode, branch=branch)
            if state.missing:
                expect = self._recorded_branch_sha(run_dir, man, branch)
            factory = WT.recreate if state.missing else WT.ensure
            kwargs = {"expect_head": expect} if state.missing else {}
            wt = factory(
                self.operator_root,
                main_root,
                slug=slug,
                run_id=run_id,
                branch=branch,
                common_dir=common,
                **kwargs,
            )
            work_root = wt.path
            if not self._record_worktree_adopted(
                run_dir, wt, slug=slug, run_id=run_id
            ):
                # The tree is real and registered, so resolver rule 1 keeps
                # answering `dedicated` and this drive is correct. What is lost
                # is rule 2's BACKSTOP: if the tree later vanishes together
                # with its registration, nothing records that it was ever
                # adopted and the run would fall back to the operator's
                # checkout (review F-001). Not worth parking a live drive over
                # — but never silent either, so it lands as a durable warning
                # `status` surfaces.
                self._warn(
                    run_dir,
                    "worktree adoption could not be journalled for run "
                    f"{run_id!r}; the tree at {wt.path} is in use and correct, "
                    "but if it is later deleted AND unregistered this run will "
                    "resolve `same_tree` instead of recreating it. Check the "
                    "journal directory is writable.",
                )
        paths = RunPaths(
            repo_root=self.repo_root,
            work_root=work_root,
            state_root=run_dir,
            artifact_root=layout.slug_dir,
        )
        self._paths = paths
        try:
            if dedicated:
                self._sync_governed_artifacts(paths, layout)
            yield paths
        finally:
            self._paths = prior

    @contextmanager
    def _worktree_paths_or_park(
        self,
        layout: "RunLayout",
        run_dir: Path,
        man: Manifest,
        *,
        mode: str,
        same_tree: bool = False,
    ):
        """:meth:`_run_paths`, converting an unavailable tree into a park (§13).

        The fail-closed fallback, and it is **operator-chosen, never
        automatic**. When the tree cannot be created, locked or verified the run
        does not proceed and does not quietly move to the operator's checkout —
        it stays exactly where it was, records `worktree_unavailable` durably as
        a manifest warning so ``status`` can name it, and raises with the one
        safe executable action R1 requires: ``gauntlet resume <slug>
        --same-tree``.

        No manifest *status* is fabricated. A resume that cannot start leaves
        the run in the state it was already in — inventing a `parked` status
        with no parked step record would put a shape into the manifest that no
        reader expects, which is the opposite of data-over-inference.
        """
        try:
            with self._run_paths(
                layout, run_dir, man,
                slug=man.slug, run_id=man.run_id, branch=man.branch,
                mode=mode, same_tree=same_tree,
            ) as paths:
                yield paths
        except WT.WorktreeUnavailable as exc:
            self._record_worktree_unavailable(run_dir, man, exc)
            raise WorktreeUnavailableError(
                f"{exc}\n\nThe run has NOT been moved or modified. Safe action: "
                f"{exc.action}  (drive this run in the operator's checkout "
                "instead — spike §13: the fallback is operator-chosen, never "
                "automatic)."
            ) from exc

    @staticmethod
    def _record_worktree_unavailable(
        run_dir: Path, man: Manifest, exc: "WT.WorktreeUnavailable"
    ) -> None:
        """Persist the park reason so ``status`` names it, not just the shell.

        Deduplicated: a repeated failure must not grow the warning list without
        bound (an operator retrying five times should see one reason, not five).
        """
        note = f"{WT.PARK_REASON_WORKTREE_UNAVAILABLE}: {exc}  Safe action: {exc.action}"
        try:
            if note in man.warnings:
                return
            man.warnings.append(note)
            man.write_atomic(run_dir / "manifest.json")
        except (OSError, ValueError):
            pass  # the raise below still names the reason and the action

    @staticmethod
    def _warn(run_dir: Path, note: str) -> None:
        """Add a durable manifest warning that ``status`` surfaces. Deduplicated.

        For conditions that must not stay silent but must not halt a live drive
        either — the FR-10.3 warnings array is exactly this channel. Best-effort
        on its own write: a warning that cannot be persisted must never be the
        thing that fails the verb it was describing.
        """
        try:
            man = Manifest.load(run_dir / "manifest.json")
            if note in man.warnings:
                return
            man.warnings.append(note)
            man.write_atomic(run_dir / "manifest.json")
        except (OSError, ValueError):
            pass

    def _release_for_same_tree_fallback(
        self, run_dir: Path, man: Manifest | None, *, slug: str, branch: str
    ) -> None:
        """Free the run branch so a `--same-tree` resume can actually check it out.

        The executable half of the §13 fallback (F-010). Removes only a tree
        this engine owns at this run's derived path; anything else is a refusal
        with the holder named, because silently removing a worktree a human
        made would be exactly the "never adopt what you cannot explain" rule
        (§11 row 6) inverted.
        """
        try:
            entry = WT.observe(
                self.operator_root, branch, main_root=self._main_worktree_root()
            )
        except gitops.GitError:
            return
        if entry is None:
            return  # nothing holds the branch — the fallback is already clear
        ref = WT.release(
            self.operator_root,
            entry.path,
            slug=slug,
            run_id=(man.run_id if man is not None else "unknown"),
        )
        self._record_worktree_released(
            run_dir, entry.path, slug=slug,
            run_id=(man.run_id if man is not None else "unknown"),
            snapshot_ref=ref,
        )

    def _recorded_branch_sha(
        self, run_dir: Path, man: Manifest | None, branch: str
    ) -> str | None:
        """The branch SHA this run's authoritative state last recorded (A3).

        Preference order, most authoritative first: the journal's own
        ``WorktreeAdopted`` ``branch_sha`` (written when the tree was last
        healthy), then the manifest's last recorded commit. ``None`` when
        neither is available — an unverifiable recreate is still better than
        refusing to recreate at all, and the caller simply skips the check
        rather than inventing an expectation.
        """
        try:
            for evt in reversed(J.read_events(run_dir)):
                if evt.get("kind") == "WorktreeAdopted":
                    sha = (evt.get("payload") or {}).get("branch_sha")
                    if sha:
                        return str(sha)
        except (OSError, ValueError, J.JournalError):
            pass
        if man is not None and man.commits:
            return man.commits[-1].sha
        return None

    def _sync_governed_artifacts(self, paths: RunPaths, layout: "RunLayout") -> None:
        """Publish the operator's governed artifacts into the run worktree.

        Spike §14.2 option A, ratified: **the operator's checkout stays the
        authoring surface.** The human authors and hand-edits `prd.md`/`plan.md`
        there, the engine reads, validates and hashes them from there
        (``artifact_root`` never moves, §4.4), and this copies the current bytes
        into the run's tree so the run branch commits what the human actually
        wrote.

        Copy, never link: §14.2 option C (symlinking the artifact dir in) was
        rejected because the run worktree gets ``reset --hard``, and a symlinked
        tracked path under a hard reset is how you lose the human's file.

        This does NOT bypass R9. Whether an approved artifact may change at all
        is decided by the governance path on the operator's copy, unchanged;
        this only makes the run's tree agree with the copy that decision was
        made about. On a `same_tree` run the two are the same file and this is
        never called.
        """
        dest_dir = paths.artifact_root_in_work
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in ("prd.md", "plan.md"):
            src = layout.slug_dir / name
            if not src.exists():
                continue
            dest = dest_dir / name
            text = src.read_text()
            if dest.exists() and dest.read_text() == text:
                continue
            dest.write_text(text)

    # The two journal kinds that record a run's tree lifecycle. Counted
    # together to form the GENERATION below, because they alternate: the Nth
    # transition of either kind is a distinct event from the (N-2)th.
    _WORKTREE_LIFECYCLE_KINDS = ("WorktreeAdopted", "WorktreeReleased")

    def _lifecycle_generation(self, run_dir: Path) -> int:
        """How many worktree lifecycle transitions this run has already recorded.

        The disambiguator in both idempotency keys (review F-002). Without it a
        key is a function of (run, path, head, transition-kind) — all of which
        REPEAT once migration and rollback made the lifecycle a cycle rather
        than a one-way trip:

        * migrate → rollback → migrate at an unchanged head produced the same
          adoption key twice, so the second adoption was silently deduplicated
          and the journal's last word was ``WorktreeReleased`` — a run whose
          tree later vanished with its registration would then resolve
          `same_tree` and drive the operator's checkout;
        * the release key never carried the head at all, so the SECOND rollback
          was always deduplicated, leaving an open adoption and a resume that
          rebuilt the tree the operator had just removed.

        Derived from the journal rather than counted in memory, so it survives
        a crash and is identical for every process that reads the same run. A
        genuine retry (the append failed and the verb is re-run) recomputes the
        SAME generation and therefore the same key, which is what keeps
        exactly-once intact; only a transition that actually landed advances it.
        """
        try:
            return sum(
                1 for evt in J.read_events(run_dir)
                if evt.get("kind") in self._WORKTREE_LIFECYCLE_KINDS
            )
        except (OSError, ValueError, J.JournalError):
            # Unreadable journal: fall back to a key that cannot collide with a
            # recorded generation. `_journal_has_key` below then reports the
            # event as unrecorded and the caller fails closed, which is the
            # right direction — we cannot prove what this run's history is.
            return -1

    def _journal_has_key(self, run_dir: Path, key: str) -> bool:
        """Whether the journal durably carries an event with ``key``.

        :func:`J.append_audit` is best-effort BY CONTRACT — it swallows I/O
        errors and returns ``False`` for both "already recorded" and "could not
        record", which are opposite outcomes. Every caller that needs the
        transition to be durable therefore re-reads rather than trusting the
        return value, and an unreadable journal answers ``False`` (fail closed).
        """
        try:
            return any(
                evt.get("idempotency_key") == key for evt in J.read_events(run_dir)
            )
        except (OSError, ValueError, J.JournalError):
            return False

    def _record_worktree_adopted(
        self, run_dir: Path, wt: "WT.RunWorktree", *, slug: str, run_id: str,
        migrated: bool = False,
    ) -> bool:
        """Journal a ``WorktreeAdopted`` event when a tree is created/recreated.

        Returns whether the transition is DURABLY recorded — ``True`` when
        nothing was due. Callers for whom the answer is the whole point (the
        explicit `migrate-worktree` transaction) fail closed on ``False``;
        see :meth:`_journal_has_key` for why the return of ``append_audit``
        cannot be used directly.

        Only on a transition — adopting an already-healthy tree on every
        subsequent verb is not an event, it is the steady state, and journaling
        it would bury the two transitions that matter (first adoption, and the
        §11-row-2 recreate) under one event per verb.

        ``migrated=True`` is P7c-2's explicit `migrate-worktree`, which appends
        the SAME kind (the seam doc §4/§5 requires it: the resolver's rule 2
        keys on the kind, and a second kind would have to be taught to every
        reader). The payload records WHICH transition it was, because "was this
        run born dedicated or moved there by a human?" is a question a future
        debugger will have and cannot otherwise answer — data over inference.
        ``prior_lock_path`` is spike §10 step 5's third payload field; it is
        recorded as observed and is deliberately UNCHANGED by migration (§8.3
        keeps the per-run drive lock in the operator's checkout precisely so
        ``driver_info`` can answer with the run worktree missing), so it
        documents where the lock was rather than where it moved.
        """
        if not (wt.created or wt.recreated):
            return True
        try:
            head = gitops.head_sha(wt.path)
        except gitops.GitError:
            head = None
        payload = {
            "slug": slug,
            "run_id": run_id,
            "branch": wt.branch,
            "path": str(wt.path),
            "recreated": wt.recreated,
            "branch_sha": head,
        }
        if migrated:
            payload["migrated"] = True
            payload["prior_lock_path"] = str(self._run_lock_path(run_dir))
        # Keyed on the transition AND the generation: re-adopting the same tree
        # within one generation is not a new event (so a retried verb cannot pad
        # the journal), but the second migration of a run that was rolled back
        # IS one, and used to collide with the first (F-002).
        key = (
            f"worktree-adopted:{run_id}:{self._lifecycle_generation(run_dir)}:"
            f"{wt.path}:{head}:"
            + ("migrate" if migrated
               else ("recreate" if wt.recreated else "create"))
        )
        J.append_audit(
            run_dir, "WorktreeAdopted", payload,
            run_id=run_id, idempotency_key=key,
        )
        return self._journal_has_key(run_dir, key)

    def _record_worktree_released(
        self, run_dir: Path, path: Path, *, slug: str, run_id: str,
        snapshot_ref: str | None = None,
    ) -> bool:
        """Journal a ``WorktreeReleased`` event after a teardown (spike §10).

        Returns whether the transition is durably recorded. The generation is
        load-bearing here in the same way as for adoption, and more so: this key
        never carried a head, so before F-002 EVERY release after the first was
        deduplicated into the first one.
        """
        key = (
            f"worktree-released:{run_id}:"
            f"{self._lifecycle_generation(run_dir)}:{path}"
        )
        J.append_audit(
            run_dir,
            "WorktreeReleased",
            {
                "slug": slug,
                "run_id": run_id,
                "path": str(path),
                "snapshot_ref": snapshot_ref,
            },
            run_id=run_id,
            idempotency_key=key,
        )
        return self._journal_has_key(run_dir, key)

    def _matches_committed_object(
        self, operator_root: Path, local: Path, ref: str, rel: str
    ) -> bool:
        """True iff ``local`` is the SAME GIT OBJECT as ``ref:rel``.

        Bytes are only one plane (review F-002). A `100755` local file and a
        `100644` blob compare byte-equal and are different objects, and the
        plan's §7 matrix lists the executable bit and the regular-file/symlink
        distinction as independently recoverable state. So the mode is compared
        too, and a symlink on either side is never a match — restoring it as a
        regular file would silently convert one kind of entry into another.
        """
        try:
            committed = gitops.file_bytes_at_commit(operator_root, ref, rel)
            mode = gitops.file_mode_at_commit(operator_root, ref, rel)
        except gitops.GitError:
            return False
        if committed is None or mode is None:
            return False
        try:
            if not local.is_file() or local.is_symlink():
                return False
            if local.read_bytes() != committed:
                return False
        except OSError:
            return False
        executable = bool(local.stat().st_mode & 0o111)
        return mode == ("100755" if executable else "100644")

    def _untracked_merge_collisions(
        self, layout: "RunLayout", man: Manifest, operator_root: Path, base: str
    ) -> tuple[list[str], list[str]]:
        """``(identical, divergent)`` untracked files the landing would overwrite.

        Only non-empty under `dedicated`, and only for the governed artifacts —
        the exact set the operator authors in their checkout (§14.2 option A)
        and the run branch commits from the copy the sync published into the run
        worktree. Git refuses outright when an untracked working-tree file would
        be overwritten, and it refuses **even when the bytes are identical**
        (measured), so this is the normal end state of a dedicated run, not an
        edge case.

        **Two refusers, not one** (review F-003). `finish` both `checkout`s the
        recorded base and then merges the run branch, and EITHER can refuse. A
        path untracked on the branch the operator happens to be standing on but
        tracked at the base is refused by the *checkout* — which runs after the
        run worktree has already been released, so the failure is unrecoverable
        by retrying and needs manual git. So a collision is anything untracked
        here and tracked at the run branch **or** at the base, and it is
        classified before anything is destroyed.

        A collision is ``identical`` only when the local file is the same git
        object (bytes AND mode, :meth:`_matches_committed_object`) as EVERY ref
        that would write it. Requiring agreement from both refs is what keeps
        the resolution honest: quarantining a file that matches the branch but
        not the base would let the checkout replace the operator's copy with a
        third version they never saw. Everything else is ``divergent`` — a real
        disagreement about an approved artifact, which only a human settles
        (R9), so it refuses early.
        """
        if self._effective_worktree_mode(man) != WT.MODE_DEDICATED:
            return ([], [])
        identical: list[str] = []
        divergent: list[str] = []
        for rel in governed_artifact_paths(self.operator_root, layout.slug_dir):
            local = self.operator_root / rel
            if not local.exists():
                continue
            try:
                if gitops.is_tracked(operator_root, rel):
                    continue  # already tracked here — an ordinary merge
                writers = [
                    ref
                    for ref in (f"refs/heads/{man.branch}", f"refs/heads/{base}")
                    if gitops.any_tracked_at(operator_root, ref, [rel])
                ]
            except gitops.GitError:
                continue
            if not writers:
                continue
            same = all(
                self._matches_committed_object(operator_root, local, ref, rel)
                for ref in writers
            )
            (identical if same else divergent).append(rel)
        return (identical, divergent)

    def _refuse_on_untracked_merge_collision(
        self, layout: "RunLayout", man: Manifest, operator_root: Path, base: str
    ) -> None:
        """Refuse a finish whose merge git would reject on a DIVERGENT untracked file.

        Detection only, and deliberately early — before the worktree release, so
        a finish that cannot complete has destroyed nothing. The identical half
        is quarantined by :meth:`_quarantine_identical_merge_collisions` just
        before the checkout+merge that restores it.
        """
        _, divergent = self._untracked_merge_collisions(
            layout, man, operator_root, base
        )
        if not divergent:
            return
        listing = "\n  ".join(divergent)
        raise FinishError(
            "refusing finish: your checkout has untracked file(s) that landing "
            f"{man.branch!r} on {base!r} would overwrite, and they are NOT the "
            "same object as the committed copy (bytes or file mode differ):\n"
            f"  {listing}\n"
            "These are the governed artifacts you authored. A dedicated run "
            "commits them on its own branch (from the copy synced into the run "
            "worktree). An identical duplicate would be replaced for you; a "
            "divergent one is a real disagreement about an approved artifact, "
            "and only you can say which version wins (R9).\n"
            "Compare with:\n"
            f"  git -C {self.operator_root} diff --no-index -- "
            f"<(git -C {self.operator_root} show {man.branch}:{divergent[0]}) "
            f"{divergent[0]}\n"
            "Resolve by either:\n"
            f"  git -C {self.operator_root} add {' '.join(divergent)} && "
            "git commit -m 'author prd'   # keep yours as a commit on the base\n"
            f"  rm {' '.join(str(self.operator_root / c) for c in divergent)}"
            "   # take the run branch's copy\n"
            "then `gauntlet finish` again."
        )

    # Suffix for the set-aside copy of a governed artifact during a landing.
    # Deliberately visible and greppable: if a crash ever strands one, an
    # operator who lists the directory sees a named file rather than a mystery.
    QUARANTINE_SUFFIX = ".gauntlet-finish-backup"

    def _quarantine_identical_merge_collisions(
        self, layout: "RunLayout", man: Manifest, operator_root: Path, base: str
    ) -> list[tuple[str, Path]]:
        """Set aside untracked duplicates PROVEN identical to what git will write.

        Called immediately before the checkout+merge and nowhere else, and
        resolved by :meth:`_settle_quarantined` on every exit path.

        **Move, never delete** (review F-002, and R2's "preserve before
        mutation"). An unlink is unrecoverable if anything downstream fails, and
        it races: between proving the bytes match and removing the file, an
        operator's editor can save a new version that the unlink then destroys.
        A rename is atomic and keeps whatever bytes existed at that instant, so
        the identity is re-proven on the QUARANTINED copy — the object we
        actually hold — and a file that changed under us is put straight back
        and refused.

        **Why this is not "the engine deletes a human's file."** The set-aside
        object is proven identical (bytes and mode) to a blob every ref that
        would write the path already carries, git restores it at that same path
        as a *tracked* file, and the copy is only removed once that succeeded.
        The operator's disk ends the verb byte-identical to how it started.
        P7c-1.1 declined this and was right in the terms it used: *"silently
        removing a human's file to make a verb succeed is the wrong default."*
        The objection is to **silently**, and to acting without proof — so this
        proves identity twice and the caller names every path in the result.

        Re-derives the collision set rather than trusting the early detection:
        the worktree release runs in between, and a re-check costs one `ls-tree`
        and one `show` per governed artifact.
        """
        identical, _ = self._untracked_merge_collisions(
            layout, man, operator_root, base
        )
        moved: list[tuple[str, Path]] = []
        refs = [f"refs/heads/{man.branch}", f"refs/heads/{base}"]
        for rel in identical:
            live = self.operator_root / rel
            held = live.with_name(live.name + self.QUARANTINE_SUFFIX)
            try:
                if held.exists():
                    raise FinishError(
                        f"refusing finish: {held} already exists. A previous "
                        "finish set your artifact aside and did not put it "
                        "back. Inspect it, restore or remove it by hand, then "
                        "retry — the engine will not overwrite a file it "
                        "cannot explain."
                    )
                live.rename(held)
            except OSError:
                continue  # could not move it → leave it; git refuses and says so
            moved.append((rel, held))
            # Re-prove on the object we now hold, not the one we measured.
            still_same = any(
                self._matches_committed_object(operator_root, held, ref, rel)
                for ref in refs
                if gitops.any_tracked_at(operator_root, ref, [rel])
            )
            if not still_same:
                self._settle_quarantined(moved)
                raise FinishError(
                    f"refusing finish: {rel} changed while finish was setting "
                    "it aside, so it is no longer the redundant duplicate this "
                    "verb proved it was. Your file has been put back exactly as "
                    "found. Commit it, or discard it, then retry."
                )
        return moved

    @staticmethod
    def _settle_quarantined(moved: list[tuple[str, Path]]) -> list[str]:
        """Resolve every set-aside artifact; return paths whose copy was KEPT.

        The ONE exit path for a quarantine, called on success and on every
        failure alike. Making it universal is the point: a separate
        "discard on success" helper has to assume git restored the file, and
        that assumption is false on the already-merged path where no checkout
        runs — which would delete the operator's artifact outright. This asks
        the filesystem instead of assuming.

        Best-effort and never raises: the failure paths that call this are
        already carrying the real cause, and an exception here would replace it
        (P7c-1.1's `merge --abort` lesson).

        Three cases, and the middle one is why this is not a plain rename back:

        * the live path is free — move it back, the ordinary undo;
        * git has since recreated the live path (the error path checks the run
          branch back out, and that branch tracks these artifacts) with the SAME
          object we hold — the file is already restored, so the held copy is
          redundant and is dropped;
        * git recreated it with a DIFFERENT object — never overwrite. The copy
          stays beside it under its visible suffix and the caller names it, so
          the operator gets two files to compare instead of a silent choice made
          for them.
        """
        kept: list[str] = []
        for rel, held in moved:
            live = held.with_name(held.name[: -len(RunManager.QUARANTINE_SUFFIX)])
            try:
                if not held.exists():
                    continue
                if not live.exists():
                    held.rename(live)
                    continue
                same = (
                    live.is_file()
                    and not live.is_symlink()
                    and held.read_bytes() == live.read_bytes()
                    and (live.stat().st_mode & 0o111) == (held.stat().st_mode & 0o111)
                )
                if same:
                    held.unlink()
                else:
                    kept.append(str(held))
            except OSError:
                kept.append(str(held))
        return kept

    def _refuse_if_run_worktree_dirty(
        self, man: Manifest, *, verb: str, exc_type: type = FinishError,
        excludes: list[str] | None = None,
    ) -> None:
        """Refuse when the RUN's own tree has uncommitted work (F-011/F-012).

        A no-op for a `same_tree` run (the caller's own operator-tree guard
        already covers that tree) and for a run whose worktree is absent. The
        message names the tree it inspected and gives the exact `git -C` form,
        because under `dedicated` the operator's `git status` reads CLEAN while
        the verb refuses on dirtiness — a contradiction they cannot resolve from
        where they are standing (spike §18.2 addition 2/3).

        ``exc_type`` lets a verb raise its OWN refusal class from the shared
        check (P7c-2: `migrate-worktree --rollback` raises
        :class:`MigrateWorktreeRefused`). The alternative — reusing
        :class:`FinishError` from a verb that is not `finish` — would put a
        class the CLI error boundary maps to a different verb's contract in
        front of the operator. One implementation, one message, the caller's
        own type.

        ``excludes`` are repo-relative paths this check must not read as dirt.
        The refusal exists to protect a BUILDER's uncommitted work (F-011's
        words) — the engine's own two-file bookkeeping export is not that: it
        is write-only with zero readers and is regenerated on the next drive,
        so a rollback taken between migration and the first checkpoint commit
        must not be blocked by the export the migration itself just wrote.
        Every other engine surface already excludes exactly this set via
        ``run_bookkeeping_excludes``; passing it here keeps this check
        consistent with them rather than uniquely stricter.
        """
        try:
            entry = WT.observe(
                self.operator_root, man.branch, main_root=self._main_worktree_root()
            )
        except gitops.GitError:
            return
        if entry is None or not entry.path.is_dir():
            return
        work_root = entry.path  # the RUN's tree — a work root by construction
        try:
            dirt = gitops.status_porcelain(
                work_root, exclude=excludes or [], untracked_all=True
            )
        except gitops.GitError:
            return
        if not dirt:
            return
        listing = "\n  ".join(dirt.splitlines()[:8])
        raise exc_type(
            f"refusing {verb}: the RUN WORKTREE has uncommitted changes.\n"
            f"  Tree inspected: {entry.path}\n  {listing}\n"
            f"  Inspect it with: git -C {entry.path} status\n"
            f"Your own checkout is clean — this work is in the run's private "
            f"tree, and {verb} would remove that tree. Commit or discard it "
            "there first (a snapshot would preserve it, but only as a recovery "
            "ref you would have to know to look for)."
        )

    def _release_run_worktree_for_slug(self, layout: "RunLayout") -> None:
        """:meth:`_release_run_worktree` for ``clean``, which may have no run.

        ``clean`` legitimately runs when the active-run pointer is already
        stale and there is no loadable manifest — that is spike §11 row 3, "a
        stale worktree whose run is gone", and it is exactly the case where the
        tree most needs removing. So the branch name is derived from config
        rather than read from a manifest, and a missing run dir is not an error.
        """
        branch = f"{self.config.branch_prefix}{layout.slug}"
        try:
            entry = WT.observe(
                self.operator_root, branch, main_root=self._main_worktree_root()
            )
        except gitops.GitError:
            return
        if entry is None:
            return
        try:
            run_dir = layout.active_run_dir()
            man = Manifest.load(run_dir / "manifest.json")
        except (OSError, ValueError, FileNotFoundError, UnsafeRunSegment):
            # Row 3: no run to journal against. Remove the tree anyway — it is
            # the orphan this verb exists to clear — and skip the event, which
            # would have nowhere authoritative to land.
            WT.release(
                self.operator_root, entry.path,
                slug=layout.slug, run_id=entry.head or "unknown",
            )
            return
        self._release_run_worktree(run_dir, man)

    def _release_run_worktree(
        self, run_dir: Path, man: Manifest, *, excludes: list[str] | None = None
    ) -> None:
        """Tear this run's worktree down, in the order git and R2 require.

        Called by the verbs that END a run's relationship with its branch —
        ``clean``, ``finish`` and ``abort``. Ordering is load-bearing (problem
        D / spike E2-D): with a live worktree on the branch, ``branch -D``
        hard-refuses, so the tree must be unlocked and removed FIRST. A dirty
        tree is snapshotted before any ``--force`` (§11 row 10 / R2).

        A no-op for a `same_tree` run and for a `dedicated` run whose tree is
        already gone — both are the steady state, not a failure.
        """
        entry = None
        try:
            # Scoped (see `WT.observe`): unscoped this would return the
            # OPERATOR's checkout for a `same_tree` run and then remove it.
            entry = WT.observe(
                self.operator_root, man.branch, main_root=self._main_worktree_root()
            )
        except gitops.GitError:
            return
        if entry is None:
            return
        ref = WT.release(
            self.operator_root,
            entry.path,
            slug=man.slug,
            run_id=man.run_id,
            excludes=excludes,
        )
        self._record_worktree_released(
            run_dir, entry.path, slug=man.slug, run_id=man.run_id, snapshot_ref=ref
        )

    def _bookkeeping_root(self, run_dir: Path) -> Path:
        """The committable bookkeeping dir for ``run_dir``, IN THE WORK TREE.

        ``run_dir`` itself same-tree; the run worktree's two-file export dir
        under `dedicated` (spike §4.4). Every bookkeeping path builder in this
        class asks THIS rather than passing ``run_dir`` straight through —
        under `dedicated` the run dir is not in the tree we commit in, so the
        builders would (correctly) fail closed instead of answering.
        """
        if self._paths is None:
            return run_dir
        return RunPaths(
            repo_root=self._paths.repo_root,
            work_root=self._paths.work_root,
            state_root=run_dir,
            artifact_root=self._paths.artifact_root,
        ).bookkeeping_root

    def _artifact_root_in_work(self, layout: "RunLayout") -> Path:
        """The governed artifacts' location in the work tree (spike §14.2)."""
        if self._paths is None:
            return layout.slug_dir
        return RunPaths(
            repo_root=self._paths.repo_root,
            work_root=self._paths.work_root,
            state_root=self._paths.state_root,
            artifact_root=layout.slug_dir,
        ).artifact_root_in_work

    def layout(self, slug: str) -> RunLayout:
        return RunLayout(self.repo_root, self.config, slug)

    @staticmethod
    def _ensure_slug_gitignore(layout: "RunLayout") -> None:
        """Ignore the slug-level live bookkeeping (BOOTSTRAP-NOTES #33).

        Idempotent; engine-owned so the guarantee never depends on the repo's
        own .gitignore. Two bookkeeping entries: the active-run pointer, and the
        slug ``.gitignore`` itself — it is engine-regenerated each run, never a
        commit payload, and leaving it untracked would dirty the worktree at the
        very first review handoff of a `standard` run (prd-cycle is step 1, with
        no commit step before it to sweep it in — unlike the bootstrap pipeline,
        whose first step is a phase commit). Self-ignoring mirrors the run-dir's
        own ``*`` self-ignore. prd.md/plan.md and manual records stay tracked."""
        layout.slug_dir.mkdir(parents=True, exist_ok=True)
        gi = layout.slug_dir / ".gitignore"
        existing = gi.read_text().split() if gi.exists() else []
        wanted = [".gitignore", "active-run.txt"]
        if any(w not in existing for w in wanted):
            lines = list(dict.fromkeys(existing + wanted))  # dedup, stable order
            gi.write_text("\n".join(lines) + "\n")

    # ---- new (FR-8.1 scaffold) ----------------------------------------------
    def new(self, slug: str) -> Path:
        layout = self.layout(slug)
        layout.slug_dir.mkdir(parents=True, exist_ok=True)
        if not layout.prd_path.exists():
            # Source the stub from the single resolved template (§4.3) and refuse
            # to scaffold from a malformed one (FR-2.1/§4.4/FR-3.3): a broken
            # gate-input template must never seed a new PRD.
            template, src = prd_stub.resolve_stub_template(
                self.repo_root, self.config.asset_root
            )
            manifest = prd_stub.resolve_manifest(self.repo_root, self.config.asset_root)
            prd_stub.validate_template(template, manifest, source=src)
            layout.prd_path.write_text(template)
        return layout.prd_path

    # ---- entry contract (FR-10.1) -------------------------------------------
    def check_entry_contract(self, slug: str) -> None:
        layout = self.layout(slug)
        if not layout.prd_path.exists():
            raise EntryContractError(
                f"{layout.prd_path} does not exist; `gauntlet new {slug}` scaffolds "
                "a stub for a human to author (FR-10.1)"
            )
        # Resolve the SAME stub template `new` would write (§4.3) and validate it
        # against the §4.4 invariants first (FR-3.3): a malformed gate-input
        # template is treated as "cannot prove human-authored", never "authored".
        template, src = prd_stub.resolve_stub_template(
            self.repo_root, self.config.asset_root
        )
        manifest = prd_stub.resolve_manifest(self.repo_root, self.config.asset_root)
        prd_stub.validate_template(template, manifest, source=src)

        content = layout.prd_path.read_text()
        if PRD_STUB_MARKER in content:
            raise EntryContractError(
                f"{layout.prd_path} is still the scaffolded stub; a human must "
                "author the PRD before a run can start (FR-10.1)"
            )
        # FR-2.4 authored-content predicate: deleting only the marker (or editing
        # only comments / headings / whitespace) leaves the scaffold un-authored.
        if not prd_stub.has_authored_content(content, template):
            raise EntryContractError(
                f"{layout.prd_path} is the scaffolded stub with no authored "
                "content (only the marker removed and/or trivial comment/heading/"
                "whitespace edits); a human must author a real PRD before a run "
                "(FR-10.1)"
            )

    def _resolve_base_branch(self) -> str:
        """Resolve ``config.base_branch``, expanding the ``current`` sentinel.

        ``base_branch: current`` means "branch from whatever I'm on", so a run
        stacks on an integration branch without a per-run flag. Fail closed on a
        detached HEAD — there is no branch name to record or merge back into.
        """
        raw = (self.config.base_branch or "").strip()
        if raw.lower() in _BASE_CURRENT_SENTINELS:
            cur = gitops.current_branch(self.operator_root)
            if cur == "HEAD":
                raise EntryContractError(
                    "base_branch is 'current' but HEAD is detached; check out a "
                    "branch to run from before `gauntlet run`"
                )
            return cur
        return raw

    def _prepare_run_branch(
        self, branch: str, base: str, *, dedicated: bool = False
    ) -> None:
        """Put the worktree on a clean run branch ``branch`` based on ``base``.

        ``dedicated`` (P7c) mints the branch WITHOUT checking it out: the run
        worktree does not exist yet, and the tree that would otherwise receive
        the checkout is the operator's — the one acceptance A1 says a start must
        never touch. The run worktree is then created ON the branch by
        ``worktree add`` (spike §6.2), which is also what supplies A2 for free.

        Fail-closed branch lifecycle (replaces a bare ``checkout``, which once
        silently rewound a worktree onto a stale branch):

        * absent            -> create it off ``base``.
        * merged into base  -> spent; discard and recreate fresh off ``base``.
          (After ``finish``/merge into the base, re-running the slug self-heals.)
        * unmerged/divergent -> REFUSE. The branch carries commits not in
          ``base``; adopting it could rewind the tree or stack on stale work.
          The human resolves it (`gauntlet clean`, merge, or rename).
        """
        repo = self.work_root
        if not gitops.branch_exists(repo, branch):
            if dedicated:
                gitops.create_branch(self.operator_root, branch, base)
            else:
                gitops.checkout_or_create_branch(repo, branch, base)
            return
        if gitops.is_ancestor(repo, branch, base):
            if dedicated:
                # `branch -f`, not `checkout -B`: there is no tree to check it
                # out into yet, and checking it out in the OPERATOR's tree is
                # exactly what acceptance A1 forbids. Git refuses `-f` outright
                # if the branch is live in any worktree (spike E2-E), which is
                # the correct fail-closed answer for a spent branch that is
                # somehow still checked out.
                gitops.create_branch(self.operator_root, branch, base, force=True)
            else:
                gitops.recreate_branch(repo, branch, base)
            return
        raise StaleRunBranchError(
            f"run branch {branch!r} already exists with commits not in base "
            f"{base!r}; refusing to adopt it (it may be a stale or unfinished "
            f"run). Run `gauntlet clean {branch.split('/')[-1]}` to discard it "
            "if it is merged elsewhere, or merge/rename it, then retry."
        )

    def _refuse_if_active_run(self, layout: "RunLayout") -> None:
        """Fail closed if a non-terminal run already owns this slug (review).

        `start()` mints a fresh run dir and overwrites ``active-run.txt``. If the
        existing active run is still running or parked, doing so silently
        orphans it — abandoning its manifest and potentially launching competing
        agents against one worktree, which breaks the clean-handoff invariant.
        Require the active run to be terminal (done/aborted/failed) or an
        explicit ``gauntlet resume`` / ``gauntlet abort`` first.

        A dangling or corrupt pointer (no manifest, unreadable JSON) is not a
        live run, so we let `start()` replace it rather than wedging the slug.
        """
        if not layout.active_pointer.exists():
            return
        try:
            man = Manifest.load(layout.active_run_dir() / "manifest.json")
        except (OSError, ValueError):
            return
        if man.status not in _TERMINAL_RUN_STATES:
            raise ActiveRunError(
                f"run {man.run_id!r} for slug {layout.slug!r} is still "
                f"{man.status!r}; refusing to start a second run that would "
                "orphan it. Use `gauntlet resume` to continue it, or "
                "`gauntlet abort` to end it first."
            )

    # ---- worktree-scoped active-run lock (FR-10.5, the one sanctioned engine
    # change alongside the run-id handshake) ----------------------------------
    #
    # `_refuse_if_active_run` (above) is the *per-slug* orphan guard: it stops a
    # `start` from clobbering a parked/running run of the **same** slug, read
    # from that slug's `active-run.txt`. It does NOT stop slug A from being
    # driven while slug B is driving the same worktree — and it is moot while a
    # run is parked (the lock is released at a gate). The lock below is the
    # complementary, *worktree-global* guard FR-10.5 adds: exactly one lockfile
    # per repo/worktree, so holding it for one slug blocks every driving verb
    # for every slug by construction. The two coexist (D7).
    #
    # ---- P7b: two scopes, one record (spike §8.3) ---------------------------
    #
    # P7b introduces the per-run driving lock at
    # `<run_root>/<slug>/<run-id>/.driving.lock` and RETAINS the worktree-global
    # guard above, at its existing path, until P7c retires it. Both are written
    # from ONE `_LockRecord` (one nonce) per acquisition.
    #
    # Why retain rather than replace. The spike's §8.3 lock model is written for
    # the END state, where every run has its own tree and git's own
    # one-branch-one-worktree rule (E2-A/E2-B) supplies cross-run exclusion for
    # free. That rule does not exist yet: through P7b every run still drives the
    # operator's checkout. Demoting the lock to a per-run path alone would let
    # slug A and slug B drive one tree concurrently — and two concurrent
    # `gauntlet run <same-slug>` invocations mint DIFFERENT run ids, so they
    # would take different per-run paths and both proceed, with only the racy
    # `active-run.txt` check between them. That is a direct regression of the
    # FR-10.5 mutual-exclusion guarantee, so the tree guard stays.
    #
    # Why it stays at the SAME path. Mutual exclusion is only worth what the
    # contending set is worth: every process that can drive this tree must
    # contend on the same object. A machine with two Gauntlet versions installed
    # (spike §8.3's "reversal cost", §10's half-migrated machine) has a pre-P7b
    # engine that knows exactly one lock path. Moving the tree guard to a new
    # name would make that engine's driving verbs blind to a live P7b driver.
    # Keeping it where it is means the old engine and the new engine exclude
    # each other in BOTH directions — which is what §10's "a half-migrated
    # machine cannot double-drive" is actually asking for.
    #
    # What P7c retires. Once `worktree.mode: dedicated` gives each run its own
    # tree, cross-slug exclusion on one tree stops being the thing to protect
    # and the tree guard becomes the legacy read-only path §10 describes: read
    # (so a legacy `same_tree` run still blocks), never written.

    def _run_root_dir(self) -> Path:
        return self.repo_root / self.config.run_root

    def _tree_lock_path(self) -> Path:
        """The retained worktree-global guard: `<run_root>/.driving.lock`.

        One per repo/worktree. Holding it for any slug blocks every driving verb
        for every slug — the property that keeps two runs off one shared tree,
        and the path a pre-P7b engine also contends on.
        """
        return self._run_root_dir() / DRIVING_LOCK_NAME

    @staticmethod
    def _run_lock_path(run_dir: Path) -> Path:
        """The per-run driving lock: `<run_root>/<slug>/<run-id>/.driving.lock`.

        Lives in the run-instance dir — in the OPERATOR's checkout, not in the
        run's tree — because `driver_info` must be able to answer "is a driver
        alive?" when the run worktree is missing, which is precisely the
        recovery case P7 exists to make survivable (spike §8.3).
        """
        return run_dir / DRIVING_LOCK_NAME

    @staticmethod
    def _ensure_run_root_gitignore(run_root: Path) -> None:
        """Ignore the worktree-level bookkeeping under the run root (FR-10.5).

        The lockfile (and the supervisor's bootstrap dir) live at the run root,
        a sibling of the slug dirs — untracked, they would dirty the worktree at
        the very first review handoff and break the clean-handoff invariant. A
        self-ignoring ``<run_root>/.gitignore`` (which lists itself) keeps them
        out of ``git status``; it never ignores tracked artifacts. Idempotent,
        engine-owned so the guarantee never depends on the repo's own ignore
        rules — mirroring :meth:`_ensure_slug_gitignore`.
        """
        run_root.mkdir(parents=True, exist_ok=True)
        gi = run_root / ".gitignore"
        existing = gi.read_text().split() if gi.exists() else []
        wanted = [
            ".gitignore",
            DRIVING_LOCK_NAME,
            DRIVING_LOCK_NAME + ".*",  # the transient acquire temp files
            ".serve-bootstrap/",
        ]
        if any(w not in existing for w in wanted):
            lines = list(dict.fromkeys(existing + wanted))  # dedup, stable order
            gi.write_text("\n".join(lines) + "\n")

    @staticmethod
    def _ensure_run_dir_gitignore(run_dir: Path) -> None:
        """Make a run-instance dir invisible to git BEFORE anything is written in it.

        The per-run lock is the first file to land in a run dir, and it lands
        *before* the Orchestrator's ``_ignore_run_dir`` runs (that is the
        Orchestrator's job, and the Orchestrator does not exist yet at
        acquisition time). Without this, the lock would be briefly visible to
        ``git status`` and would dirty the tree ahead of the first clean-handoff
        guard (FR-9.3) — the invariant in CLAUDE.md §1.

        Writes the same self-ignoring ``*`` marker ``_ignore_run_dir`` writes, so
        the two are idempotent with respect to each other and whichever runs
        first wins.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        gitignore = run_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")

    def _read_lock(self, run_dir: Path | None = None) -> _LockRecord | None:
        """The drive-lock record for a run: per-run path first, tree guard second.

        P7b writes both files from one record, so for a run driven by a P7b
        engine either answers. The fallback is what makes a **legacy** run —
        started by a pre-P7b engine, which only ever wrote the tree guard —
        readable by this engine without migrating anything (spike §10).

        ``run_dir=None`` reads the tree guard alone (the acquisition-time view,
        and the pre-P7b behaviour of this method).

        A per-run lock that EXISTS but is unreadable does **not** fall through
        (review F-002): consulting a different file would answer confidently
        about a run whose own evidence we could not read. It returns ``None``,
        which every caller here treats as a fail-closed refusal.
        """
        return self._read_lock_state(run_dir)[1]

    def _read_lock_state(
        self, run_dir: Path | None = None
    ) -> tuple[str, _LockRecord | None]:
        """:meth:`_read_lock`, keeping the tri-state kind (review F-002).

        Callers that must distinguish "no lock" from "a lock I could not read"
        take this. :meth:`_read_lock` discards the kind, which is safe only
        where every not-a-record outcome is already handled identically.
        """
        if run_dir is not None:
            kind, rec = locking.read_lock_state(self._run_lock_path(run_dir))
            if kind != locking.LOCK_ABSENT:
                return (kind, rec)  # present → record; malformed → no fallback
        return locking.read_lock_state(self._tree_lock_path())

    # The reclaim rule itself now lives in `engine.locking` so the tree guard,
    # the per-run lock and the repo-global git lock cannot drift apart. These
    # stay as methods because the semantics are part of this class's contract
    # (and its tests reach for them by name).
    _lock_is_live = staticmethod(locking.record_is_live)
    _link_into_place = staticmethod(locking.link_into_place)

    @staticmethod
    def _lock_busy_message(rec: _LockRecord) -> str:
        who = f"{rec.slug}/{rec.run_id}" if rec.run_id else rec.slug
        return (
            f"worktree is being driven by {who} (pid {rec.pid}); wait, or "
            "abort that run first (FR-10.5)"
        )

    def _new_lock_record(self, slug: str, run_id: str | None) -> _LockRecord:
        return locking.new_record(slug, run_id)

    def _try_reclaim(
        self, lock_path: Path, observed: _LockRecord, nonce: str, payload: str
    ) -> bool:
        """Reclaim a lock we PROVED stale; True iff we now hold it.

        ``observed`` is a parsed record the caller has already proven dead or
        PID-reused. This re-reads **``lock_path`` itself** immediately before
        removing it (P7b: the caller may be reclaiming either scope, so this
        must never re-read some other lock) and unlinks only that record
        (matching nonce) — never a *new* owner's fresh lock (the F-004 inverse
        of ownership-validated release), and never a record it could not read
        (review F-002: an unreadable lock may belong to a LIVE driver, so
        "cannot read" must never mean "may delete"). Then races to atomically
        link our record into place; a lost race (someone else reclaimed first)
        returns ``False`` so the caller re-evaluates the holder.
        """
        kind, current = locking.read_lock_state(lock_path)
        if kind == locking.LOCK_MALFORMED:
            return False  # unreadable now → cannot prove staleness; fail closed
        if kind == locking.LOCK_PRESENT:
            assert current is not None
            if self._lock_is_live(current):
                return False  # became live (or a fresh owner) → caller fails closed
            if current.nonce != observed.nonce:
                return False  # changed under us → re-evaluate, don't blind-unlink
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
        # LOCK_ABSENT: it vanished under us — nothing to unlink, just race for it.
        return self._link_into_place(lock_path, nonce, payload)

    def _acquire_one(
        self, lock_path: Path, record: _LockRecord, payload: str
    ) -> None:
        """Take ONE lock file or fail closed (FR-10.5); no handle bookkeeping.

        Atomic create-if-absent via ``os.link`` so check-and-acquire has no
        TOCTOU window and the lock is never observed empty. The three outcomes
        of the shared tri-state read (review F-002) are handled distinctly:

        * **present + live** → fail the verb closed, regardless of slug;
        * **present + provably dead/reused** → reclaim as stale;
        * **malformed** (exists but unreadable or unparseable) → fail closed
          with a named remedy. This is the case that used to be collapsed into
          "corrupt → unlink it": an unreadable lock can belong to a *live*
          driver, and stealing it re-opens double-driving. It is also what
          ``operator.driver_info`` already reports as ``indeterminate``, so the
          read-only view and the mutating path now agree (R4);
        * **absent** → it vanished under us; loop and race for the link.
        """
        for _ in range(locking.LOCK_ACQUIRE_RETRIES):
            if self._link_into_place(lock_path, record.nonce, payload):
                return
            kind, existing = locking.read_lock_state(lock_path)
            if kind == locking.LOCK_MALFORMED:
                raise WorktreeLockError(locking.malformed_lock_message(lock_path))
            if kind == locking.LOCK_PRESENT:
                assert existing is not None
                if self._lock_is_live(existing):
                    raise WorktreeLockError(self._lock_busy_message(existing))
                if self._try_reclaim(lock_path, existing, record.nonce, payload):
                    return
            # transient race (a concurrent reclaim, or it vanished) → re-evaluate
        raise WorktreeLockError(
            "could not acquire the worktree lock after repeated reclaim races "
            f"({lock_path}); a driver may be churning — fail closed (FR-10.5)"
        )

    def _acquire_worktree_lock(
        self, slug: str, run_id: str | None, *, run_dir: Path | None = None
    ) -> _LockHandle:
        """Acquire the drive lock for a run and fail closed (FR-10.5).

        Takes the worktree-global **tree guard** first — the coarse, still
        load-bearing exclusion while every run shares the operator's checkout —
        then, when ``run_dir`` is given, the **per-run** lock inside that run's
        instance dir. One record, one nonce, published at both paths. The order
        is fixed (tree then run) at every call site, and the tree guard is
        released if the per-run acquisition fails, so no partial hold survives.

        ``run_dir=None`` is the `start()` case: the run dir does not exist yet
        at acquisition time, so `start` attaches the per-run lock with
        :meth:`_attach_run_lock` the moment it mints the dir — still under the
        tree guard, and still before any agent runs. Deliberately NOT solved by
        creating the run dir here: `start` can legitimately refuse after
        acquiring (a still-active run, a dirty worktree), and an empty
        `run-<ts>/` left behind by a refusal would become the
        lexicographically-greatest instance that `resolve_run_instance` picks
        when there is no `active-run.txt`.

        Acquired **first** by `start`/`resume`/`approve`, before any run dir /
        `active-run.txt` / git mutation.
        """
        run_root = self._run_root_dir()
        run_root.mkdir(parents=True, exist_ok=True)
        self._ensure_run_root_gitignore(run_root)
        record = self._new_lock_record(slug, run_id)
        payload = record.to_json()
        tree_path = self._tree_lock_path()
        self._acquire_one(tree_path, record, payload)
        handle = self._take_handle(tree_path, record.nonce)
        if run_dir is not None:
            try:
                self._attach_run_lock(handle, run_dir, record=record)
            except BaseException:
                self._release_worktree_lock(handle)
                raise
        return handle

    def _attach_run_lock(
        self, handle: _LockHandle, run_dir: Path, *, record: _LockRecord | None = None
    ) -> None:
        """Publish this acquisition's record at the per-run path too (P7b).

        Idempotent for a handle that already carries a ``run_path``. The run dir
        is created and self-ignored FIRST, so the lock never dirties the tree
        (see :meth:`_ensure_run_dir_gitignore`).
        """
        if handle.run_path is not None:
            return
        self._ensure_run_dir_gitignore(run_dir)
        if record is None:
            # Re-derive the published record so the two files stay byte-identical
            # (same pid/pgid/identity/started_at), not merely same-nonce.
            record = locking.read_record(handle.path)
        if record is None or record.nonce != handle.nonce:
            raise WorktreeLockError(
                f"the tree guard at {handle.path} no longer carries this "
                "acquisition's nonce; refusing to publish a per-run lock under "
                "an ownership we cannot prove (FR-10.5)"
            )
        run_path = self._run_lock_path(run_dir)
        self._acquire_one(run_path, record, record.to_json())
        handle.run_path = run_path

    def _take_handle(self, lock_path: Path, nonce: str) -> _LockHandle:
        handle = _LockHandle(path=lock_path, nonce=nonce)
        self._held_lock = handle
        atexit.register(self._release_worktree_lock, handle)
        return handle

    def _release_worktree_lock(self, handle: _LockHandle | None) -> None:
        """Release the lock, but only if it still carries our nonce (F-004).

        If a file now holds a different nonce, we were already reclaimed as
        stale and a *new* owner is driving — unlinking would re-open
        double-driving, so that release is a **no-op**. Idempotent: safe to call
        from the per-verb ``finally`` and again from the atexit fallback.

        Releases the per-run lock BEFORE the tree guard — the reverse of
        acquisition — so no window exists in which another process can take the
        tree guard while this run's per-run lock still looks held.
        """
        if handle is None:
            return
        if handle.run_path is not None:
            locking.unlink_if_nonce(handle.run_path, handle.nonce)
        locking.unlink_if_nonce(handle.path, handle.nonce)
        if self._held_lock is handle:
            self._held_lock = None
        # No-op if already gone; clears the atexit fallback for this manager
        # (it holds at most one lock at a time, so this never drops a live one).
        atexit.unregister(self._release_worktree_lock)

    # ---- run (FR-8.1) -------------------------------------------------------
    def start(
        self,
        slug: str,
        pipeline_path: Path,
        *,
        use_judge: bool = True,
        adapter_factory=None,
        extra_context: dict | None = None,
        clock=None,
        run_id: str | None = None,
        reservation_token: str | None = None,
    ) -> str:
        # Containment first (FR-10.1 / review F-001): slug and a supplied run id
        # flow straight into filesystem paths below, so refuse a traversal/
        # separator/NUL segment before any path is built or any sidecar written.
        safe_run_segment(slug, kind="slug")
        if run_id is not None:
            safe_run_segment(run_id, kind="run_id")
        self.check_entry_contract(slug)
        layout = self.layout(slug)
        # Run-id allocation handshake (FR-6.1a): the console supervisor
        # pre-allocates the id and passes it as `gauntlet run --run-id <id>` so
        # it knows `run_dir` before launch and can place the captured log +
        # `job.json`. A *provided* id is single-use — error if its run dir
        # already exists; a *minted* id disambiguates a (rare) same-second
        # restart with a suffix.
        #
        # NOTE (UPSTREAM CONFLICT, surfaced not worked-around): FR-6.1a also
        # names "the GAUNTLET_RUN_ID env var" as an equivalent handshake input.
        # That name is ALREADY taken by the judge (judge/hook_client.py
        # RUN_ID_ENV_VAR) to tell an agent's PreToolUse hooks which run they
        # belong to, and the engine exports it into os.environ during every
        # judged run. Reading it here would make `start()` silently inherit a
        # stale/ambient run id from the surrounding session. The `--run-id` flag
        # (the §6 control-surface + FR-6.1a primary mechanism) is collision-free
        # and is what the supervisor uses, so the env-var equivalent is left
        # unwired pending human resolution of the name clash.
        provided = run_id
        if provided:
            run_id = provided
            # Single-use (FR-6.1a). A supplied id may reuse a pre-existing run
            # dir ONLY when it is the supervisor's fresh, single-use reservation
            # for this very launch: the supervisor writes a reservation token
            # under `run_dir/.serve/` and passes it as `--reservation-token`
            # *before* launching this child (it also pre-creates `.serve/` for
            # the captured log + job.json). Any other pre-existing run dir —
            # a prior run's manifest, or a failed launch's diagnostic
            # sidecar/log with no matching token — is refused so its state is
            # never reused or overwritten (review F-005).
            rd = layout.run_dir(run_id)
            if (rd / "manifest.json").exists():
                raise ActiveRunError(
                    f"run {run_id!r} already exists for slug {slug!r}; a "
                    "pre-allocated --run-id must be single-use (FR-6.1a)"
                )
            if rd.exists() and not _reservation_matches(rd, reservation_token):
                raise ActiveRunError(
                    f"run dir for {run_id!r} already exists for slug {slug!r} "
                    "with prior run/diagnostic state and no matching fresh "
                    "reservation; a pre-allocated --run-id must be single-use "
                    "(FR-6.1a)"
                )
        else:
            run_id = f"run-{_utc_stamp()}"
            suffix = 1
            while (layout.run_dir(run_id) / "manifest.json").exists():
                run_id = f"run-{_utc_stamp()}-{suffix}"
                suffix += 1

        # Acquire the worktree-global tree guard FIRST — before any run dir /
        # active-run.txt / git mutation (FR-10.5). Released in `finally` on
        # park/done/error. The per-run lock (P7b) is attached below, the moment
        # the run dir exists; until then this guard alone excludes every other
        # driving verb on this tree, including a second `start` of this slug.
        handle = self._acquire_worktree_lock(slug, run_id)
        try:
            self._refuse_if_active_run(layout)
            pipeline, phash = load_pipeline(pipeline_path)
            # FR-1.3: pass the repo/artifact roots so reference/`phase` context
            # inputs are containment- and existence-checked before any step runs.
            validate_pipeline(
                pipeline, self.config,
                repo_root=self.repo_root, artifact_root=layout.slug_dir,
            )

            base_branch = self._resolve_base_branch()
            branch = f"{self.config.branch_prefix}{slug}"
            # F-3: the base must be an integration branch, never a machine-owned
            # run branch. `base: current` while on a gauntlet/* branch would
            # otherwise record branch==base and later wedge `finish`.
            if base_branch == branch or base_branch.startswith(self.config.branch_prefix):
                raise BaseBranchError(
                    f"base resolves to a run branch {base_branch!r} (prefix "
                    f"{self.config.branch_prefix!r}); check out an integration "
                    "branch to run from (or set base_branch) — the base must "
                    "not be a gauntlet/* branch"
                )
            # #61: refuse a dirty worktree BEFORE the run branch exists.
            # `checkout -b` carries uncommitted changes onto the fresh
            # gauntlet/* branch, and the first clean-handoff guard (FR-9.3)
            # then fails the run — stranding the operator on a half-born
            # branch they must hand-delete. The exclusion policy is exactly
            # the one the drive itself applies (PR #75 review, both rounds:
            # preflight and clean-handoff must never disagree, or the
            # stranded-branch bug returns through the gap): the shared
            # bookkeeping excludes (this run's instance dir + EVERY slug's
            # human-owned PR.md, PRD §2.2) plus EXACTLY this slug's prd.md —
            # the one artifact legitimately uncommitted at start, which the
            # first cycle baseline-commits (FR-5.1). Not the whole slug dir:
            # the baseline fires only when the SINGLE dirty path is the
            # artifact itself, so any extra slug-dir file (notes.md, a stale
            # plan.md) would pass a dir-wide exemption and then fail the
            # handoff guard after the branch existed. Sibling-slug artifacts
            # are refused for the same reason.
            preflight_excludes = run_bookkeeping_excludes(
                self.repo_root, layout.run_dir(run_id), layout.slug_dir
            )
            try:
                preflight_excludes.append(
                    layout.prd_path.resolve()
                    .relative_to(self.repo_root.resolve())
                    .as_posix()
                )
            except ValueError:
                pass
            # `untracked_all` so untracked files are listed individually — in
            # `normal` mode git may collapse a fully-untracked directory into a
            # single `dir/` entry the exclude pathspecs cannot suppress, and
            # the refusal message would be less actionable (see the
            # status_porcelain docstring).
            # The #61 guard exists because `checkout -b` CARRIES uncommitted
            # changes onto the fresh run branch. Under `dedicated` nothing is
            # checked out in the operator's tree at all — the branch is minted
            # from the base ref and a fresh worktree is checked out at the
            # derived path — so the operator's dirt cannot ride onto the run
            # branch and cannot fail the first clean-handoff guard. Refusing on
            # it would be a guard against a mechanism that no longer exists,
            # and it would block a start for a reason the operator could not
            # act on (spike §9.4 flags this as the behaviour change to
            # document). The prd.md the run needs reaches the run branch
            # through the §14.2 sync instead, not through the checkout.
            born_dedicated = self.configured_worktree_mode == WT.MODE_DEDICATED
            if not born_dedicated:
                dirt = gitops.status_porcelain(
                    self.work_root, exclude=preflight_excludes, untracked_all=True
                )
                if dirt:
                    listing = "\n  ".join(dirt.splitlines()[:8])
                    raise WorktreeDirtyError(
                        f"refusing to start {slug!r}: the worktree has "
                        "uncommitted changes beyond this run's prd.md:\n"
                        f"  {listing}\n"
                        "Commit, stash, or discard them first — starting now "
                        f"would create {branch!r} carrying these changes and "
                        "fail the first clean-handoff guard (FR-9.3), leaving a "
                        "half-initialized run branch to clean up by hand (#61). "
                        "(A pending PR.md from a finished run is exempt and "
                        "never blocks a start.)"
                    )
            self._prepare_run_branch(branch, base_branch, dedicated=born_dedicated)

            run_dir = layout.run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            # P7b: publish this acquisition's record at the per-run path now that
            # the dir exists, so `driver_info`, `recover` and the recovery
            # executor's lock guard can all find THIS run's driver at the
            # per-run location for the whole drive. Still under the tree guard,
            # still before any agent runs. `_attach_run_lock` writes the run
            # dir's self-ignoring `.gitignore` first, so the lock never dirties
            # the tree ahead of the first clean-handoff guard.
            self._attach_run_lock(handle, run_dir)
            # The active-run pointer is live bookkeeping, never commit payload
            # (BOOTSTRAP-NOTES #33). An engine-written slug-level .gitignore
            # keeps it ignored in EVERY repo — including throwaway fixture repos
            # that lack the init-provided `runs/*/active-run.txt` rule — so it
            # never dirties the worktree and `git add` never collides with it.
            self._ensure_slug_gitignore(layout)
            # Snapshot the exact pipeline source into the run dir so resume
            # reloads precisely what started the run (FR-5.6 reproducibility).
            (run_dir / "pipeline.yaml").write_text(pipeline_path.read_text())
            layout.active_pointer.write_text(run_id)

            man = Manifest(
                run_id=run_id,
                slug=slug,
                branch=branch,
                # Record the RESOLVED base (never the `current` sentinel) so
                # resume, the PR draft, and `finish` act on a concrete branch.
                base_branch=base_branch,
                pipeline=PipelineRef(name=pipeline.name, version=pipeline.version, hash=phash),
                prompt_hashes=self._prompt_hashes(pipeline),
                # The ONE place `config.worktree.mode` is read (spike §13 /
                # `proposals/P7c-split-seam.md` §3): it decides what a NEW run
                # is born as, and is then recorded so no later verb has to
                # consult live config — which is what makes flipping the config
                # on a repo with existing runs incapable of moving them.
                worktree_mode=self.configured_worktree_mode,
            )
            with self._worktree_paths_or_park(
                layout, run_dir, man, mode=man.worktree_mode
            ):
                status = self._drive(
                    layout, run_dir, pipeline, man,
                    use_judge=use_judge, adapter_factory=adapter_factory,
                    extra_context=extra_context, clock=clock,
                )
        finally:
            self._release_worktree_lock(handle)
        # Auto-resume runs OUTSIDE the lock (each attempt re-acquires it via
        # `_resume_once`) so the wait between attempts holds no worktree lock —
        # matching the reconciliation model (FR-3.4). A no-op unless the run
        # parked on a usage limit under `resume_on_quota: auto`.
        return self._auto_resume_if_scheduled(
            slug, status, use_judge=use_judge, adapter_factory=adapter_factory,
            extra_context=extra_context, clock=clock,
        )

    # ---- resume (FR-8.2) ----------------------------------------------------
    def resume(self, slug: str, *, response: str | None = None,
               use_judge: bool = True, adapter_factory=None,
               extra_context: dict | None = None, clock=None,
               auto_sleep=None, reset_interrupted: bool = False,
               same_tree: bool = False) -> str:
        """One resume, then in-process auto-resume of a usage-limit park (FR-3.4).

        A manual resume always continues the session once immediately (the
        "manual override resumes now" branch): that is ``_resume_once``. If the
        run re-parks on the usage limit under ``resume_on_quota: auto``, the live
        driver waits until the projected reset and resumes again, bounded by
        ``max_auto_resume_attempts`` — :meth:`_auto_resume_if_scheduled`. In
        ``notify`` mode the wrapper is a no-op.
        """
        # R5 (plan §4.5): fingerprint the persisted state before and after the
        # whole verb — an unchanged repeat that is not a legitimate live wait
        # raises NoProgressError (nonzero) instead of exiting 0 re-parked.
        before = self._capture_progress(slug)
        status = self._resume_once(
            slug, response=response, use_judge=use_judge,
            adapter_factory=adapter_factory, extra_context=extra_context, clock=clock,
            reset_interrupted=reset_interrupted, same_tree=same_tree,
        )
        # NOTE: reset_interrupted is deliberately NOT forwarded to the
        # auto-resume continuation — it is a one-shot operator decision for
        # THIS resume, never a standing policy (#72). `same_tree` is withheld
        # for the same reason and a stronger one: it is the fallback for a
        # worktree that was unavailable at THIS moment, so carrying it into an
        # unattended continuation would keep driving the operator's checkout
        # long after the condition cleared — silently, which is the one thing
        # spike §13 says the fallback must never be.
        status = self._auto_resume_if_scheduled(
            slug, status, use_judge=use_judge, adapter_factory=adapter_factory,
            extra_context=extra_context, clock=clock, sleep=auto_sleep,
        )
        self._require_progress_after(slug, before, verb="resume")
        return status

    def _resume_once(self, slug: str, *, response: str | None = None,
                     use_judge: bool = True, adapter_factory=None,
                     extra_context: dict | None = None, clock=None,
                     reset_interrupted: bool = False,
                     same_tree: bool = False) -> str:
        layout = self.layout(slug)
        self._ensure_slug_gitignore(layout)  # idempotent (#33; old runs too)
        run_dir = layout.active_run_dir()
        # P6: the journal is authoritative — reconcile the manifest projection
        # FIRST (catch-up / adoption / executor rebuild, plan §4.6/§5.5/R8),
        # so every load below reads the authoritative state.
        self._reconcile_projection(run_dir, slug)
        # FR-5.6 crash reconciliation runs on this mutating entry point BEFORE the
        # lock is touched: finalization compares a surviving intent's nonce against
        # the lock the wedged driver left, and acquiring the lock first (which
        # reclaims a stale dead-driver lock under a *fresh* nonce) would destroy
        # that comparison and mislabel a finalize-able intent as stale. Reload the
        # manifest after, so a finalized recovery's INTERRUPTED step is what drives.
        self._reconcile_recovery_intent(run_dir)
        man = Manifest.load(run_dir / "manifest.json")
        # Resume is a driving verb (FR-10.5): take the worktree lock FIRST,
        # before the branch checkout / drive. The lock record carries this run's
        # id from the manifest so a concurrent verb's refusal names the holder.
        handle = self._acquire_worktree_lock(slug, man.run_id, run_dir=run_dir)
        try:
            # Resolve THIS run's tree before anything observes or mutates one.
            # The mode comes from evidence + what the run was born as, never
            # from live config — see `_effective_worktree_mode`. `--same-tree`
            # is the operator's one-shot override for a run whose worktree is
            # unavailable (spike §13); it drives this resume in the operator's
            # checkout and is never persisted as a mode change.
            mode = self._effective_worktree_mode(man)
            with self._worktree_paths_or_park(
                layout, run_dir, man, mode=mode, same_tree=same_tree
            ) as paths:
                return self._resume_locked(
                    layout, run_dir, man, paths,
                    response=response, use_judge=use_judge,
                    adapter_factory=adapter_factory, extra_context=extra_context,
                    clock=clock, reset_interrupted=reset_interrupted,
                )
        finally:
            self._release_worktree_lock(handle)

    def _resume_locked(
        self, layout: "RunLayout", run_dir: Path, man: Manifest, paths: RunPaths,
        *, response: str | None, use_judge: bool, adapter_factory,
        extra_context: dict | None, clock, reset_interrupted: bool,
    ) -> str:
        """The body of one resume, with the drive lock held and roots resolved."""
        # P3 (plan §4.3): a recovery transaction killed between its intent
        # persist and its intent clear left a durable, replayable intent.
        # Resume is a mutating command, so it converges that intent FIRST —
        # idempotently, under the lock — before driving anything new; an
        # unrecognized repository state fails closed with named evidence.
        replayed = RX.replay_pending_intent(self.work_root, run_dir)
        if replayed is not None:
            man = Manifest.load(run_dir / "manifest.json")  # finisher wrote
        pipeline, phash = load_pipeline(run_dir / "pipeline.yaml")
        if phash != man.pipeline.hash:
            raise RuntimeError(
                "pipeline content hash changed since the run started "
                f"({man.pipeline.hash} -> {phash}); resume refuses to run a "
                "different pipeline against an existing manifest (FR-5.6)"
            )
        # F-1: resume continues the SAME branch the run committed to. Never
        # recreate it from base (the old checkout_or_create_branch) — that
        # would silently drop the manifest's recorded commits. P4 (plan
        # §5.4/R6): the branch↔manifest relationship is now assessed by the
        # SAME observation machinery status renders — an inventoried,
        # proven relation — and reconciled by class: bookkeeping tolerated,
        # checkpoint/implementation/operator ranges ADOPTED (loud manifest
        # audit, no rewind), behind/forked/missing refused with the
        # assessment's executable recovery actions named. Everything is
        # validated against the branch REF *before* checkout, so a refusal
        # never rewinds the worktree onto a stale/reset branch.
        repo = self.work_root
        git_obs = self._observe_resume_branch(layout, run_dir, man)
        relation = RX.BranchRelation
        if git_obs.branch_relation is relation.MISSING:
            raise RunBranchStateError(
                f"resume: run branch {man.branch!r} is missing; recreating "
                "it from base would drop the manifest's recorded commits. "
                "Restore the branch (e.g. from refs/gauntlet/backup/) "
                "first." + self._relation_action_detail(git_obs, man)
            )
        if git_obs.branch_relation in (relation.BEHIND, relation.FORKED):
            last = git_obs.recorded_sha or ""
            raise RunBranchStateError(
                f"resume: branch {man.branch!r} is missing the "
                f"manifest's recorded commit {last[:10]} (reset or "
                "recreated); the branch and manifest disagree. "
                "Reconcile (restore the branch, or `gauntlet rollback`) "
                "before resuming."
                + self._relation_action_detail(git_obs, man)
            )
        # Under `dedicated` the run worktree was created ON this branch and
        # is the only worktree that may hold it (git's one-branch-one-
        # worktree rule, spike E2-A), so "ensure the tree is on the branch"
        # is already true and a checkout here would be a no-op at best. Under
        # `same_tree` this is still the line that puts the operator's
        # checkout on the run branch (spike §9.4).
        if not paths.dedicated_worktree:
            gitops.checkout_branch(repo, man.branch)
        # Linear branch-ahead reconciliation (P4, plan §5.4 / issue #72):
        # adopt a proven checkpoint/implementation/operator range into the
        # manifest — loud, manifest-only, never a Git mutation — so a
        # builder killed after committing but before the manifest flush
        # resumes without rollback or git surgery.
        adoption_notes = RX.reconcile_branch_ahead(man, git_obs, verb="resume")
        if adoption_notes:
            man.write_atomic(run_dir / "manifest.json")
        # Plan the --response transition (FR-1/FR-1.1/FR-8/FR-9 guards +
        # FR-7.1 idempotent recovery). All validation and operator-identity
        # resolution happen HERE, before driving; the orchestrator only
        # applies an already-validated, fail-closed decision.
        action = self._plan_response_action(man, response, pipeline)
        return self._drive(
            layout, run_dir, pipeline, man,
            use_judge=use_judge, adapter_factory=adapter_factory,
            extra_context=extra_context, clock=clock,
            response_action=action,
            interrupted_override="reset_to_base" if reset_interrupted else None,
        )

    def _observe_resume_branch(
        self, layout: "RunLayout", run_dir: Path, man: Manifest
    ) -> "RX.GitObservation":
        """The P1 Git observation resume reconciles against (P4, plan §5.4).

        The recorded boundary is the in-flight attempt's ``base_sha`` when a
        step is mid-attempt (that is what the resume disposition diffs
        against), else the manifest's last recorded commit. Read-only; a
        merge commit inside the range fails closed through
        :class:`RX.RecoveryObservationError` with named evidence.
        """
        excludes = run_bookkeeping_excludes(
            self.work_root, self._bookkeeping_root(run_dir), layout.slug_dir
        )
        return RX.observe_git(
            self.work_root,
            run_branch=man.branch,
            recorded_sha=RX.reconciliation_boundary(man),
            excludes=excludes,
            bookkeeping_candidates=engine_bookkeeping_candidates(
                self.work_root, self._bookkeeping_root(run_dir)
            ),
            approved_artifacts=governed_artifact_paths(
                self.work_root, self._artifact_root_in_work(layout)
            ),
        )

    # ---- R5: no successful no-op loops (P4, plan §4.5) ----------------------
    def _progress_fingerprint_for(
        self, layout: "RunLayout", run_dir: Path, man: Manifest
    ) -> "RX.ProgressFingerprint":
        """The plan §4.5 fingerprint at the public-verb boundary."""
        excludes = run_bookkeeping_excludes(
            self.work_root, self._bookkeeping_root(run_dir),
            self._artifact_root_in_work(layout),
        )
        record = None
        for rec in man.steps:  # the last non-terminal step anchors the attempt
            if rec.status not in (M.DONE, M.SKIPPED):
                record = rec
        # The R5 fingerprint covers index and worktree planes, so it must watch
        # the tree the verb actually mutates (same family as F-002/F-007).
        # Against the operator's checkout it would report "nothing changed"
        # after a dedicated resume that did real work, and raise NoProgressError
        # on a run that had progressed.
        return RX.build_progress_fingerprint(
            self.work_root, manifest=man, record=record, excludes=excludes
        )

    def _capture_progress(self, slug: str) -> "RX.ProgressFingerprint | None":
        """Best-effort pre-verb fingerprint; ``None`` disables the guard.

        Advisory capture: a repo-less run tree or an unreadable manifest must
        never mask the verb's own behavior, so any observation failure simply
        skips the R5 guard for this invocation.
        """
        try:
            layout = self.layout(slug)
            run_dir = layout.active_run_dir()
            man = Manifest.load(run_dir / "manifest.json")
            return self._progress_fingerprint_for(layout, run_dir, man)
        except (OSError, ValueError, gitops.GitError, RX.RecoveryExecError):
            return None

    def _require_progress_after(
        self,
        slug: str,
        before: "RX.ProgressFingerprint | None",
        *,
        verb: str,
        exempt_human_waits: bool = True,
    ) -> None:
        """Raise :class:`NoProgressError` on an unchanged fingerprint (R5).

        A mutating public verb (resume/recover/rollback) that returns to an
        identical progress fingerprint without entering a legitimate live wait
        must exit nonzero naming what is unchanged and listing executable safe
        actions — never print only ``run status: parked`` and exit zero on an
        unchanged repeat (plan §4.5). Legitimate waits are exempt: a terminal
        run, a quota/usage-window park (a provider deadline; FR-3.2/FR-10.3),
        an armed scheduled resume (FR-3.4), and — for resume — a park awaiting
        a human decision (a gate or ``--response`` park; R7 decisions are
        semantic, and their surfaces already name the exact verb).
        """
        if before is None:
            return
        try:
            layout = self.layout(slug)
            run_dir = layout.active_run_dir()
            man = Manifest.load(run_dir / "manifest.json")
            after = self._progress_fingerprint_for(layout, run_dir, man)
        except (OSError, ValueError, gitops.GitError, RX.RecoveryExecError):
            return
        if after.digest != before.digest:
            return  # progress
        if man.status in (M.RUN_DONE, M.RUN_ABORTED):
            return  # terminal — nothing left to progress toward
        state, parked_rec, _failure = RX.classify_composite(
            man, RX.DriverLiveness.NONE.value
        )
        if exempt_human_waits and state in (
            RX.STATE_PARKED_GATE, RX.STATE_PARKED_FOR_RESPONSE
        ):
            return
        if parked_rec is not None:
            reason = M.normalize_parked_reason(
                parked_rec.parked_reason, parked_rec.type, parked_rec.status
            )
            # A quota/window/dependency park is a legitimate live wait ONLY
            # when it carries a concrete deadline — a recorded reset/
            # replenishment/backoff time or an armed auto-resume schedule
            # (post-review F-006; extended to provider_unavailable parks by
            # P5, plan §5.2). An unchanged park with NO deadline is
            # indistinguishable from a wedge, which is exactly the successful
            # no-op loop R5 forbids: it falls through and raises with the
            # retry/abort actions named.
            if (
                reason in (
                    M.PARKED_REASON_USAGE_LIMIT,
                    M.PARKED_REASON_USAGE_WINDOW,
                    M.PARKED_REASON_PROVIDER_UNAVAILABLE,
                )
                and parked_rec.quota_reset_at is not None
            ):
                return
            if parked_rec.scheduled_resume is not None:
                return  # an armed auto-resume schedule is a legitimate wait
        try:
            git_obs = self._observe_resume_branch(layout, run_dir, man)
        except (gitops.GitError, RX.RecoveryExecError, OSError, ValueError):
            git_obs = None
        actions: tuple = ()
        try:
            assessment = RX.RecoveryPlanner(self.repo_root).assess(
                manifest=man,
                liveness=RX.DriverLiveness.NONE.value,
                git_obs=git_obs,
                fingerprint=after,
            )
            actions = assessment.safe_actions
        except (gitops.GitError, RX.RecoveryExecError, OSError, ValueError):
            actions = ()
        if not actions:
            actions = (
                AbortAction(
                    description=(
                        f"`gauntlet abort {slug}` aborts the run, retaining "
                        "every snapshot and all evidence"
                    ),
                    reason=f"{verb} made no progress and no safer action resolved",
                ),
            )
        raise NoProgressError(before, after, tuple(actions))

    @staticmethod
    def _relation_action_detail(git_obs: "RX.GitObservation", man: Manifest) -> str:
        """Executable recovery actions for a refused branch relation (plan §5.4).

        The refusal must offer the SAME actions the read-only status surface
        renders for this relation (R4) — not only "reconcile manually" — so
        both derive from :func:`RX.relation_recovery_actions` through the one
        operator renderer.
        """
        from gauntlet.engine import operator

        try:
            state, _, _ = RX.classify_composite(man, RX.DriverLiveness.NONE.value)
            assessment = RX.RecoveryAssessment(
                cause=RX.RecoveryCause.BRANCH_DIVERGED,
                disposition=RX.RecoveryDisposition.CONTINUE_ON_RECOVERY_BRANCH,
                safe_actions=RX.relation_recovery_actions(git_obs, man),
                recommended_action=RX.relation_recovery_actions(git_obs, man)[0].kind,
                progress_fingerprint="sha256:refusal",
            )
            rendered = operator.render_assessment_actions(
                assessment, state, man.slug
            )
        except Exception:  # advisory detail must never mask the refusal
            return ""
        commands = [a.command for a in rendered if a.kind != "observe"]
        if not commands:
            return ""
        return " Safe actions: " + "; ".join(f"`{c}`" for c in commands)

    @staticmethod
    def _parked_usage_limit_step(man: Manifest) -> "M.StepRecord | None":
        """The scheduled-resume-armed usage-limit park, or ``None`` (shared find)."""
        return next(
            (
                s for s in man.steps
                if s.status == M.PARKED
                and s.parked_reason == M.PARKED_REASON_USAGE_LIMIT
                and s.scheduled_resume is not None
            ),
            None,
        )

    @contextlib.contextmanager
    def _auto_resume_wait_context(self, run_dir: Path):
        """Heartbeat + keep-awake spanning the auto-resume quota wait (FR-3.4/FR-5).

        The wait between resume attempts is a *live* driver, not a stopped one, so
        it must keep the FR-5 liveness signals up: the heartbeat writer keeps
        stamping ``heartbeat.json`` (so ``status`` reads a live driver, and a sleep
        during the wait is detected + persisted) and — opt-in — ``caffeinate`` holds
        the host awake for the wait. Scoped to the wait only: the continuation
        resume's own ``_drive`` owns the heartbeat while it runs, so the two never
        overlap on the single-slot writer registry (heartbeat._active).
        """
        from gauntlet.engine.heartbeat import HeartbeatWriter, KeepAwake

        writer = HeartbeatWriter(
            run_dir,
            interval_s=self.config.heartbeat_interval_s,
            credit_cap_s=self.config.suspend_credit_cap_s,
        )
        with KeepAwake(enabled=self.config.keep_awake), writer:
            yield

    def _arm_next_attempt(self, slug: str, run_dir: Path, run_id: str | None) -> bool:
        """Increment the parked step's auto-resume attempt count under the lock (F-005).

        Auto-resume runs outside the worktree lock (each attempt re-acquires it),
        but its manifest writes must not race a concurrent manual resume. Reload +
        revalidate under the lock so a state change between the loop's read and this
        write is not clobbered; return ``False`` (re-loop and re-decide) if the
        parked usage-limit schedule is gone or already at the ceiling.
        """
        handle = self._acquire_worktree_lock(slug, run_id, run_dir=run_dir)
        try:
            man = Manifest.load(run_dir / "manifest.json")
            step = self._parked_usage_limit_step(man)
            if step is None or step.scheduled_resume is None:
                return False
            if step.scheduled_resume.attempts >= step.scheduled_resume.max_attempts:
                return False
            step.scheduled_resume.attempts += 1
            man.write_atomic(run_dir / "manifest.json")
            return True
        finally:
            self._release_worktree_lock(handle)

    def _exhaust_schedule(self, slug: str, run_dir: Path, run_id: str | None) -> None:
        """Clear the auto-resume schedule at the ceiling under the lock (F-005).

        Same lock discipline as :meth:`_arm_next_attempt`: reload + revalidate so
        the exhaustion note never overwrites a concurrent manual resume's state.
        """
        handle = self._acquire_worktree_lock(slug, run_id, run_dir=run_dir)
        try:
            man = Manifest.load(run_dir / "manifest.json")
            step = self._parked_usage_limit_step(man)
            if step is None or step.scheduled_resume is None:
                return
            step.scheduled_resume = None
            note = (
                f"auto-resume exhausted after {self.config.max_auto_resume_attempts} "
                "attempts (FR-3.4); left as a plain usage_limit park — resume "
                "manually once the window clears, or abort"
            )
            step.notes = f"{step.notes}\n{note}" if step.notes else note
            man.write_atomic(run_dir / "manifest.json")
        finally:
            self._release_worktree_lock(handle)

    def _auto_resume_if_scheduled(
        self, slug: str, status: str, *, use_judge: bool, adapter_factory,
        extra_context: dict | None, clock, sleep=None, wait_context=None,
    ) -> str:
        """Drive the in-process auto-resume wait loop for a usage-limit park (FR-3.4).

        A no-op unless ``resume_on_quota: auto``. Reads the parked step's
        ``scheduled_resume`` (armed by the orchestrator at park time), and on each
        pass either waits for the projected reset (suspend-aware: sleeps in bounded
        polls and re-checks the wall clock), performs one continuation resume
        (incrementing ``attempts`` write-ahead so a crash never re-tries for free),
        or — at the attempt ceiling — falls back to a plain park with an exhaustion
        note. Restart-safe: it re-reads the persisted schedule from disk every
        pass, so a driver restart before/after the reset reconciles identically.

        The quota wait itself runs under a heartbeat + keep-awake context
        (``wait_context``, FR-5/F-002) so the waiting driver stays live and — opt-in
        — the host stays awake; the context is entered lazily on the first wait and
        released before each continuation resume (which owns its own heartbeat). All
        manifest mutations (attempt increment, exhaustion note) happen under the
        worktree lock (F-005) so they never clobber a concurrent manual resume.
        """
        if self.config.resume_on_quota != RESUME_ON_QUOTA_AUTO:
            return status
        _sleep = sleep or time.sleep
        _wait_ctx = wait_context or self._auto_resume_wait_context
        layout = self.layout(slug)
        wait_cm = None  # heartbeat/keep-awake held across contiguous waits only
        try:
            while True:
                try:
                    run_dir = layout.active_run_dir()
                    man = Manifest.load(run_dir / "manifest.json")
                except (FileNotFoundError, OSError, ValueError):
                    return status
                if man.status != M.RUN_PARKED:
                    return status
                step = self._parked_usage_limit_step(man)
                if step is None:
                    return status
                now = self._auto_resume_now(clock)
                action, wait_s = next_auto_resume_action(step.scheduled_resume, now)
                # Leaving the wait: release the heartbeat/keep-awake before any
                # resume so its `_drive` heartbeat does not overlap this one.
                if action != AUTO_RESUME_WAIT and wait_cm is not None:
                    wait_cm.__exit__(None, None, None)
                    wait_cm = None
                if action == AUTO_RESUME_NONE:
                    return status
                if action == AUTO_RESUME_EXHAUST:
                    try:
                        self._exhaust_schedule(slug, run_dir, man.run_id)
                    except WorktreeLockError:
                        pass  # a concurrent driver holds the lock — defer to it
                    return status
                if action == AUTO_RESUME_WAIT:
                    if wait_cm is None:
                        wait_cm = _wait_ctx(run_dir)
                        wait_cm.__enter__()
                    _sleep(min(wait_s, _AUTO_RESUME_POLL_S))
                    continue
                # AUTO_RESUME_RESUME: count the attempt write-ahead (under the
                # lock, F-005), then continue with one continuation resume.
                try:
                    armed = self._arm_next_attempt(slug, run_dir, man.run_id)
                except WorktreeLockError:
                    return status  # a concurrent driver holds the lock — defer
                if not armed:
                    continue  # state changed under us — re-read and re-decide
                status = self._resume_once(
                    slug, response=None, use_judge=use_judge,
                    adapter_factory=adapter_factory, extra_context=extra_context,
                    clock=clock,
                )
        finally:
            if wait_cm is not None:
                wait_cm.__exit__(None, None, None)

    @staticmethod
    def _auto_resume_now(clock) -> datetime:
        """The wall-clock 'now' for auto-resume timing, consistent with the clock
        the schedule's ``attempt_at`` was derived from (the orchestrator clock)."""
        if clock is not None:
            try:
                dt = datetime.fromisoformat(clock())
                return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        return datetime.now(timezone.utc)

    def _plan_response_action(
        self, man: Manifest, response: str | None, pipeline=None
    ) -> ResponseAction:
        """Validate `gauntlet resume [--response]` and decide the transition.

        Guard order is deliberate and fails closed (CLAUDE.md §2): crash
        recovery is checked FIRST (a pending entry preempts every other path),
        then the response-less scoping (FR-1.1), then the new-append guards
        (FR-1/FR-8) with operator identity resolved last (FR-9) so an
        unresolvable identity errors before anything is appended.
        """
        # FR-7.1 — recovery: a prior invocation crashed mid-transition.
        pending = self._step_with_pending_response(man)
        if pending is not None:
            latest = pending.human_responses[-1]
            if response is not None and response != latest.response_text:
                raise ValueError(
                    f"a pending response ({latest.response_id}) is awaiting "
                    f"processing; re-run `gauntlet resume {man.slug}` to finish "
                    "it, or abort the run — do not supply a new response over a "
                    "pending one."
                )
            return ResponseAction(
                kind="recover", step_id=pending.id, iteration=pending.iteration
            )

        # No pending entry.
        if response is None:
            # FR-1.1 / FR-10.4: a response-resolvable park REQUIRES --response —
            # the builder's UPSTREAM CONFLICT (agent_task) AND a reviewer-surfaced
            # cycle escalation (adversarial_cycle). Re-driving either without a
            # decision just re-runs into the same wall, which is the deadlock this
            # path exists to prevent. Every other park keeps its existing
            # response-less re-run behavior unchanged.
            parked = self._parked_step(man)
            # Normalize any legacy on-disk parked_reason to the PRD enum before
            # comparing (FR-7.2 read-side contract): a pre-P3 manifest carrying
            # `upstream_conflict`/`cycle_escalation` must still route as `response`.
            parked_reason = (
                M.normalize_parked_reason(
                    parked.parked_reason, parked.type, parked.status
                )
                if parked is not None else None
            )
            if (
                parked is not None
                and parked_reason in M.RESPONSE_RESOLVABLE_PARK_REASONS
            ):
                # Both the builder conflict and the cycle escalation collapse to
                # `response`; the agent_task-vs-cycle distinction is recovered from
                # the step type (FR-7.2), not the reason value.
                what = (
                    "an upstream conflict"
                    if parked.type == "agent_task"
                    else "a cycle escalation its own loop cannot resolve"
                )
                raise ValueError(
                    f"step '{parked.id}' parked on {what}; resume it with "
                    '--response "<decision>" (see `gauntlet resume --help`). '
                    "Re-running without a decision would only re-surface it."
                )
            # Never silently no-op a terminally FAILED run (review feedback):
            # `status` recommends `resume`, so a plain resume that declines to
            # act must say WHY and what to do — not print `run status: failed`
            # and exit 0. A re-runnable failure (a clean-handoff PRECONDITION the
            # operator has since fixed, or an on_fail retry budget) falls through
            # to re-drive; only a genuinely TERMINAL failure raises here.
            if man.status == M.RUN_FAILED and pipeline is not None:
                failed = self._failed_step(man)
                step = (
                    self._pipeline_step(pipeline, failed.id)
                    if failed is not None else None
                )
                if step is not None and Orchestrator._is_terminal_failure(step, failed):
                    detail = (failed.notes or "no further detail recorded").strip()
                    # F-007: recommend --response only for a step type the
                    # --response validator will actually accept; a terminally
                    # failed shell step / rejected gate gets the executable
                    # exit (abort) instead of a command that will be refused.
                    if failed.type in M.RESPONDABLE_STEP_TYPES:
                        way_out = (
                            "If a human decision can unblock it (e.g. "
                            "reclassifying a finding the fixer could not act "
                            f'on), inject one: `gauntlet resume {man.slug} '
                            '--response "<decision>"`. Otherwise `gauntlet '
                            "abort` the run."
                        )
                    else:
                        way_out = (
                            f"Step type {failed.type!r} accepts no `--response` "
                            f"decision; `gauntlet abort {man.slug}` is the "
                            "executable exit (all snapshots and evidence are "
                            "retained)."
                        )
                    raise ValueError(
                        f"run '{man.slug}' failed terminally at step "
                        f"'{failed.id}': {detail} A plain `gauntlet resume` cannot "
                        f"re-run a terminal failure — it would only repeat it. "
                        + way_out
                    )
            return ResponseAction(kind="none")

        # FR-1/FR-8/FR-10.5: a new --response targets the run's STUCK respondable
        # step — a PARKED step (a builder conflict or a cycle escalation) OR a
        # FAILED one (a cycle/agent_task whose execution failed, e.g. a cycle
        # whose fixer made no changes). Both are "blocked cycles" a human decision
        # can unblock: the decision is injected on the re-drive. Resolve identity
        # LAST so a fail-closed identity error (FR-9) leaves the manifest
        # untouched (no entry appended).
        if man.status not in (M.RUN_PARKED, M.RUN_FAILED):
            raise ValueError(
                f"run '{man.run_id}' is {man.status}, neither parked nor failed; "
                "cannot resume with --response"
            )
        stuck = self._parked_step(man) or self._failed_step(man)
        if stuck is None:
            raise ValueError(
                f"run '{man.run_id}' has no parked or failed step to resume with "
                "--response"
            )
        if stuck.type == "human_gate":
            raise ValueError(
                "use `gauntlet approve` or `gauntlet reject` for human_gate "
                "steps; --response is for agent_task and adversarial_cycle steps"
            )
        if stuck.type not in M.RESPONDABLE_STEP_TYPES:
            raise ValueError(
                f"step '{stuck.id}' is a {stuck.type}; --response only applies "
                f"to {' / '.join(sorted(M.RESPONDABLE_STEP_TYPES))} steps"
            )
        user = resolve_operator_identity(self.repo_root)
        return ResponseAction(
            kind="append", step_id=stuck.id, iteration=stuck.iteration,
            text=response, user=user,
        )

    @staticmethod
    def _step_with_pending_response(man: Manifest):
        """The step whose latest `--response` entry is still `pending`, if any.

        At most one is ever in flight; the last in execution order wins.
        """
        target = None
        for rec in man.steps:
            if (
                rec.human_responses
                and rec.human_responses[-1].state == M.RESPONSE_PENDING
            ):
                target = rec
        return target

    @staticmethod
    def _pipeline_step(pipeline, step_id: str):
        """The pipeline ``Step`` matching ``step_id`` (for failure classification)."""
        for step in pipeline.all_steps():
            if step.id == step_id:
                return step
        return None

    @staticmethod
    def _parked_step(man: Manifest):
        """The single parked StepRecord (the run parks one step at a time)."""
        for rec in man.steps:
            if rec.status == M.PARKED:
                return rec
        return None

    @staticmethod
    def _failed_step(man: Manifest):
        """The last FAILED StepRecord, for a `--response` resume of a failed run.

        A failed run halts at the step that failed, so the last FAILED record is
        that step. Resuming it with `--response` appends a fresh `pending` entry,
        which clears the consumed-terminal-failure guard (FR-7.1) so the step
        re-runs with the decision injected (e.g. a cycle whose fixer made no
        changes, re-driven after a human reclassifies the offending finding)."""
        for rec in reversed(man.steps):
            if rec.status == M.FAILED:
                return rec
        return None

    # ---- gates --------------------------------------------------------------
    def approve(self, slug: str, gate: str | None = None, notes: str | None = None,
                *, use_judge: bool = True, adapter_factory=None) -> str:
        explicit_gate = gate
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        self._reconcile_projection(run_dir, slug)  # P6: journal-first (plan §4.6)
        man = Manifest.load(run_dir / "manifest.json")
        # Approve drives the rest of the run, so it is a driving verb (FR-10.5):
        # take the worktree lock first, released in `finally` on the next park /
        # done / error.
        handle = self._acquire_worktree_lock(slug, man.run_id, run_dir=run_dir)
        try:
            # A surviving recovery intent from a killed transaction converges
            # before the approval drives anything (post-P3 review F-002).
            # A driving verb runs in the run's own tree (P7c). Resolved from
            # evidence + what the run was born as, never from live config.
            with self._worktree_paths_or_park(
                layout, run_dir, man, mode=self._effective_worktree_mode(man)
            ):
                if RX.replay_pending_intent(self.work_root, run_dir) is not None:
                    man = Manifest.load(run_dir / "manifest.json")
                gate = explicit_gate or man.current_step
                if gate is None:
                    raise ValueError("no gate to approve; run is not parked")
                pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
                # Approving a gate drives the rest of the run, so honor use_judge.
                if use_judge:
                    return self._with_judge(man, run_dir, lambda env: self._approve_drive(
                        layout, run_dir, pipeline, man, gate, notes, env, adapter_factory))
                orch = self._orchestrator(layout, run_dir, pipeline, man,
                                          judge_env={}, adapter_factory=adapter_factory)
                status = orch.approve_gate(gate, notes)
                self._maybe_draft_pr(layout, run_dir, man, status)
                return status
        finally:
            self._release_worktree_lock(handle)

    def reject(self, slug: str, notes: str, gate: str | None = None,
               *, use_judge: bool = True, adapter_factory=None) -> str:
        explicit_gate = gate
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        self._reconcile_projection(run_dir, slug)  # P6: journal-first (plan §4.6)
        man = Manifest.load(run_dir / "manifest.json")
        # Reject now re-drives the upstream adversarial_cycle with the rejection
        # note injected as a new round (FR-8.1 + operator playbook), so like
        # `approve` it is a driving verb: take the worktree lock first and honor
        # the judge. The rejection is attributed to the resolved operator identity.
        user = resolve_operator_identity(self.repo_root)
        handle = self._acquire_worktree_lock(slug, man.run_id, run_dir=run_dir)
        try:
            # A surviving recovery intent from a killed transaction converges
            # before the rejection re-drives anything (post-P3 review F-002).
            # A driving verb runs in the run's own tree (P7c). Resolved from
            # evidence + what the run was born as, never from live config.
            with self._worktree_paths_or_park(
                layout, run_dir, man, mode=self._effective_worktree_mode(man)
            ):
                if RX.replay_pending_intent(self.work_root, run_dir) is not None:
                    man = Manifest.load(run_dir / "manifest.json")
                gate = explicit_gate or man.current_step
                if gate is None:
                    raise ValueError("no gate to reject; run is not parked")
                pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
                if use_judge:
                    return self._with_judge(man, run_dir, lambda env: self._reject_drive(
                        layout, run_dir, pipeline, man, gate, notes, user, env,
                        adapter_factory))
                orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env={},
                                          adapter_factory=adapter_factory)
                status = orch.reject_gate(gate, notes, user)
                self._maybe_draft_pr(layout, run_dir, man, status)
                return status
        finally:
            self._release_worktree_lock(handle)

    def _reject_drive(self, layout, run_dir, pipeline, man, gate, notes, user, env,
                      adapter_factory):
        orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env=env,
                                  adapter_factory=adapter_factory)
        status = orch.reject_gate(gate, notes, user)
        self._maybe_draft_pr(layout, run_dir, man, status)
        return status

    # ---- abort --------------------------------------------------------------
    # ---- orphaned-judge reaping on cleanup verbs (FR-6) ---------------------
    def _reap_orphaned_judge(self, run_dir: Path, slug: str) -> str | None:
        """Reap the run's per-run judge iff it is *orphaned* and *ours* (FR-6).

        Called by the cleanup verbs (`abort`/`finish`/`clean`). The judge is
        signalled **iff all three** hold; any other case sends **no signal**,
        leaves ``judge.json`` intact, and returns ``None`` (fail closed, §6.4):

        (a) ``judge.json`` records a judge whose **own** identity verifies as
            ours on **this host** — :func:`procident.process_is_alive` (PID live
            + exact creation-time identity match) plus a host equality check
            (FR-6.1/FR-6.2). An absent/mismatched/``null``/unsupported-platform
            identity datum fails closed here, never a foreign or PID-reused kill.
        (b) the recorded ``pgid`` still belongs to that verified PID — a
            **positive** ``os.getpgid(pid)`` **equal** to the record (review
            F-001), so a stale or corrupted record can never steer a group
            signal at an unrelated process group.
        (c) the **owning driver is gone** — :func:`operator.driver_liveness`
            (the single sanctioned §6.4 primitive, never inferred from any other
            artifact) is ``orphaned`` or ``none``. A driver that is ``alive``
            (running) or ``indeterminate`` (liveness unprovable) → never reap.

        On all three: SIGTERM→SIGKILL the re-confirmed group (bounded grace) and
        remove ``judge.json``. Best-effort — it never raises into the calling
        verb: a verified-but-unsignalable judge (EPERM) is left in place rather
        than crashing the cleanup. It never touches the shared console (FR-6.3):
        only the group recorded in ``judge.json`` is ever a target.
        """
        from gauntlet.engine import operator

        record = read_judge_record(run_dir)
        if record is None:
            return None  # no / malformed judge.json → nothing to reap

        # (a) the judge's own identity must verify as ours on this host.
        if record.host != socket.gethostname():
            return None  # foreign-host PID in a shared run root → never signal
        identity = ProcessIdentity.from_dict(record.proc_identity)
        if not process_is_alive(record.pid, identity):
            # dead / PID-reused / null identity / unsupported platform → no kill
            return None

        # (b) the recorded pgid must still be the verified PID's group (F-001):
        # re-confirm it so a stale/corrupted record can never steer the signal.
        try:
            actual_pgid = os.getpgid(record.pid)
        except OSError:
            return None
        if actual_pgid <= 0 or actual_pgid != record.pgid:
            return None  # non-positive or mismatched group → fail closed

        # (c) the owning driver must be provably gone (orphaned/none) — §6.4.
        liveness = operator.driver_liveness(
            self._run_root_dir(), slug, run_instance_dir=run_dir
        )
        if liveness not in (operator.LIVENESS_ORPHANED, operator.LIVENESS_NONE):
            return None  # alive (running) or indeterminate → fail closed

        # All three hold: signal the re-confirmed group, then drop the record.
        try:
            outcome = _signal_process_group(record.pgid)
        except RecoverSignalError:
            # EPERM: identity proven but the OS refuses the signal — the judge is
            # still alive and unkillable by us. Leave judge.json so a privileged
            # operator can still find it; never derail the cleanup verb.
            return None
        _unlink_durable(run_dir / JUDGE_RECORD_NAME)
        return outcome

    def _reap_orphaned_judge_safe(self, layout: "RunLayout") -> None:
        """Resolve the active run dir and reap its orphaned judge, if any (FR-6).

        Wraps :meth:`_reap_orphaned_judge` for a verb that may run after the
        active-run pointer is gone (e.g. `clean` reclaiming a stale pointer): a
        missing or unsafe pointer simply means there is no run dir to read
        ``judge.json`` from, so there is nothing to reap — skip, never fail the
        verb.
        """
        try:
            run_dir = layout.active_run_dir()
        except (FileNotFoundError, UnsafeRunSegment):
            return
        self._reap_orphaned_judge(run_dir, layout.slug)

    def abort(self, slug: str) -> str:
        # Deliberately NOT a recovery-intent replay point (contrast resume/
        # rollback/approve/reject, post-P3 review F-002): abort's R1 contract
        # is "abort while retaining all snapshots and evidence" — it mutates
        # only the run status, never Git state, so a surviving intent stays in
        # place as preserved evidence for a later explicit resume/rollback.
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        self._reconcile_projection(run_dir, slug)  # P6: journal-first (plan §4.6)
        man = Manifest.load(run_dir / "manifest.json")
        # Terminal history is read-only (review F-002): never rewrite a
        # done/aborted/failed run's status. Fail closed so neither a stray CLI
        # `gauntlet abort` nor the console control path can corrupt a completed
        # run's recorded outcome.
        if man.status in _TERMINAL_RUN_STATES:
            raise AbortGuardError(
                f"run {man.run_id!r} for slug {slug!r} is already {man.status}; "
                "terminal runs cannot be aborted (history is read-only)"
            )
        man.status = M.RUN_ABORTED
        man.write_atomic(run_dir / "manifest.json")
        # The run worktree is deliberately LEFT IN PLACE (spike §11: abort may
        # remove it or keep it as evidence — this keeps it). Abort's R1
        # contract is "abort while retaining all snapshots and evidence", and a
        # run is most often aborted precisely because something went wrong in
        # that tree; deleting the one artifact an operator would want to look
        # at would contradict the verb's whole purpose. `gauntlet clean` is the
        # verb that removes it, and it does so in the E2-D-safe order.
        # FR-6: an orphaned judge left by a dead/crashed driver is reaped here
        # (identity-verified, driver-gone-only); a live run's judge is untouched.
        self._reap_orphaned_judge(run_dir, slug)
        return man.status

    # ---- recover (operator-aids P4, FR-5) -----------------------------------
    def recover(self, slug: str, *, reason: str | None = None) -> str:
        """Terminate a verified, wedged *live* driver and mark its step INTERRUPTED.

        The only mutating operator verb here (FR-5). It fills the gap ``resume``
        cannot: ``resume`` reclaims a *stale* (dead/orphaned) lock, but never a
        *live* one, so an alive-but-wedged driver's lock would block every verb
        forever. ``recover`` signals only a process it can *prove* via process
        identity is the same driver it launched — on this host, still in the
        recorded process group — marks the in-flight step ``INTERRUPTED``, appends
        an append-only §6.4 audit record, releases the lock, and stops. It does
        **not** auto-resume (separation of concerns, Non-Goal §2.2).

        Crash-consistent and idempotent via the FR-5.6 nonce-/state-guarded
        sequence; safe to interrupt at every boundary and safe to re-run.
        """
        safe_run_segment(slug, kind="slug")
        # FR-5.5 operator-only boundary (mechanism 2, authoritative for ad-hoc
        # invocation): refuse — before any reconcile, signal, or mutation — when
        # running inside a pipeline-agent context. `GAUNTLET_STEP_ID` is the
        # per-step marker the orchestrator exports to every in-run agent (the same
        # signal the judge's pipeline_step_only rules key on), so an in-pipeline
        # agent that shells out to `gauntlet recover` is refused by `recover`
        # itself, keeping the §2.2 "policy.yaml unchanged" promise true.
        if os.environ.get("GAUNTLET_STEP_ID"):
            raise RecoverRefused(
                "refusing `gauntlet recover` inside a pipeline-agent context "
                "(GAUNTLET_STEP_ID is set): recover is an operator-only action, "
                "never an in-pipeline step (FR-5.5). No signal sent. Run it from "
                "an operator session instead."
            )
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()

        # P6: journal-first projection reconciliation on this mutating verb
        # (plan §4.6/§5.5/R8), before the intent reconcile reads the manifest.
        self._reconcile_projection(run_dir, slug)
        # FR-5.6 step 0: reconcile any surviving intent from a prior interrupted
        # `recover` FIRST (this is a mutating context). A finalize-able intent is
        # finalized here; a stale one discarded — so the fresh recovery below sees
        # a clean slate.
        self._reconcile_recovery_intent(run_dir)

        # Containment (review F-003): refuse a symlinked/escaping manifest before
        # any recover read or write; reuse the proven path for every load below.
        manifest_path = self._guard_run_file(run_dir, "manifest.json")
        man = Manifest.load(manifest_path)
        before_fp = self._capture_progress(slug)  # R5 guard input

        # FR-5.6 step 1: capture the lock once and run the full FR-5.1 gate.
        captured = self._read_lock(run_dir)
        verified = self._verify_recover_target(captured, slug, run_dir=run_dir)

        # FR-5.6 step 2: state guard — the in-flight step must still be `running`.
        target = self._recover_target_step(man)

        # FR-5.6 step 3: re-read the lock immediately before persisting/signalling.
        # A changed/absent nonce means the driver finished or relaunched between
        # step 1 and now → abort WITHOUT signalling (the race against a normally
        # completing driver, closed).
        current = self._read_lock(run_dir)
        if current is None or current.nonce != verified.nonce:
            raise RecoverConcurrent(
                f"run {man.run_id!r} completed or relaunched concurrently "
                "(the drive lock changed before signalling); no action taken — "
                f"re-run `gauntlet status {slug}`."
            )

        # FR-5.6 step 4: persist the durable intent BEFORE any signal — while the
        # gate's identity proof is still valid (before the PID can become dead).
        from gauntlet.engine import operator

        actor, actor_source = self._recover_actor()
        intent = _RecoveryIntent(
            ts=_utc_stamp(),
            actor=actor,
            actor_source=actor_source,
            reason=reason,
            lock_nonce=verified.nonce,
            pid=verified.pid,
            pgid=verified.pgid,
            proc_identity=verified.proc_identity,
            host=verified.host,
            step_id=operator.render_step_id(target),
            prior_step_status=target.status,
            prior_run_status=man.status,
        )
        intent_path = run_dir / RECOVERY_INTENT_NAME
        _atomic_write_durable(intent_path, intent.to_json())

        # FR-5.6 step 4.5 (review F-004): close the TOCTOU window between the
        # step-3 nonce check and the durable intent write. The driver can complete
        # NORMALLY in that window — finishing the step, transitioning the run, and
        # releasing the lock — after which finalizing the stale in-memory manifest
        # would overwrite a completed step/run with INTERRUPTED/RUN_FAILED. Re-read
        # the lock AND reload the manifest fresh from disk immediately after the
        # write: if the nonce vanished/changed or the target is no longer
        # `running`, the driver finished or relaunched → discard the just-written
        # intent and abort WITHOUT signalling or mutating the manifest.
        recheck = self._read_lock(run_dir)
        fresh_man = Manifest.load(manifest_path)
        fresh_target = self._find_step_by_rendered_id(fresh_man, intent.step_id)
        if (
            recheck is None
            or recheck.nonce != verified.nonce
            or fresh_target is None
            or fresh_target.status != M.RUNNING
        ):
            _unlink_durable(intent_path)
            raise RecoverConcurrent(
                f"run {man.run_id!r} completed or relaunched concurrently "
                "(the driver finished after the recovery intent was written); no "
                f"signal sent, no state changed — re-run `gauntlet status {slug}`."
            )

        # FR-5.6 step 5: re-verify identity against the frozen intent, then signal
        # (closes the TOCTOU window where the PID/PGID is reused across 2–4).
        try:
            outcome = self._signal_recover_target(intent)
        except RecoverSignalError:
            # Verified but unsignalable (EPERM): the driver is still alive and was
            # NOT killed. Clear the intent we just wrote so reconciliation never
            # retries the un-killable signal forever, then surface the fail-closed
            # refusal — the manifest is untouched (review F-005).
            _unlink_durable(intent_path)
            raise

        # FR-5.6 steps 6–8: atomic INTERRUPTED + append record, clear intent,
        # release the lock under the recorded-nonce guard. `_finalize_recovery`
        # reloads the manifest fresh and re-checks the running guard, so it never
        # overwrites a concurrently-completed run.
        self._finalize_recovery(run_dir, intent, outcome)
        # R5: a recover that changed nothing (the finalize refused because the
        # target transitioned concurrently) must not exit 0 unchanged.
        self._require_progress_after(
            slug, before_fp, verb="recover", exempt_human_waits=False
        )
        return Manifest.load(manifest_path).status

    @staticmethod
    def _recover_actor() -> tuple[str, str]:
        """The invoking OS user for the §6.4 audit (``getpass.getuser``).

        Tagged ``os_user`` so the identity provenance is explicit. Audit-only —
        an unresolvable username never blocks a recovery (it is not a safety
        datum), so it falls back to ``"unknown"`` rather than failing closed.
        """
        try:
            return getpass.getuser(), "os_user"
        except Exception:  # pragma: no cover - getuser rarely fails
            return "unknown", "os_user"

    def _verify_recover_target(
        self, rec: "_LockRecord | None", slug: str, *, run_dir: Path | None = None
    ) -> "_LockRecord":
        """The full FR-5.1 identity gate (all ANDed); return the verified record.

        Every condition must hold; any failed or unobtainable datum is a
        fail-closed refusal with **no signal sent** (FR-5.1/FR-5.4). The PID-live
        + exact-identity-match + host-equality trio is computed by P1's
        :func:`operator.driver_liveness` (so ``alive`` here is exactly liveness
        ``alive``, never ``orphaned``/``indeterminate``); the PID-in-PGID check is
        the extra immediate-pre-signal gate.
        """
        from gauntlet.engine import operator

        safe = (
            "Safe alternatives: wait for the driver to finish, or inspect with "
            f"`gauntlet status {slug}` / `gauntlet logs {slug}`."
        )
        if rec is None:
            raise RecoverRefused(
                f"no drive lock is present for {slug!r}; there is no live driver "
                f"to recover. {safe}"
            )
        if rec.slug != slug:
            raise RecoverRefused(
                f"the drive lock is owned by {rec.slug!r}, not {slug!r}; refusing "
                f"to signal another run's driver. {safe}"
            )
        if rec.host != socket.gethostname():
            raise RecoverRefused(
                f"the drive lock was created on host {rec.host!r}, not this host "
                f"({socket.gethostname()!r}); refusing to signal a foreign-host "
                f"PID in a shared run root. {safe}"
            )
        liveness = operator.driver_liveness(
            self._run_root_dir(), slug, run_instance_dir=run_dir
        )
        if liveness != operator.LIVENESS_ALIVE:
            why = {
                operator.LIVENESS_ORPHANED: (
                    "the recorded driver is gone or its PID was recycled "
                    "(orphaned); use `gauntlet resume` to reclaim the stale lock"
                ),
                operator.LIVENESS_INDETERMINATE: (
                    "the driver's process identity is unobtainable/unverifiable "
                    "(indeterminate) — it cannot be proven the recorded process"
                ),
                operator.LIVENESS_NONE: "no live driver is present",
            }.get(liveness, f"driver liveness is {liveness!r}, not alive")
            raise RecoverRefused(
                f"refusing to recover {slug!r}: {why}. {safe}"
            )
        # PID-in-PGID, immediately before signalling: never signal a PGID the
        # proven-ours PID has since left (or that was never its group). An
        # unobtainable getpgid is a fail-closed refusal.
        try:
            actual_pgid = os.getpgid(rec.pid)
        except OSError as exc:
            raise RecoverRefused(
                f"refusing to recover {slug!r}: the recorded PID {rec.pid}'s "
                f"process group is unobtainable ({exc}). {safe}"
            ) from exc
        if actual_pgid != rec.pgid:
            raise RecoverRefused(
                f"refusing to recover {slug!r}: PID {rec.pid} is no longer in the "
                f"recorded process group {rec.pgid} (now {actual_pgid}); it has "
                f"regrouped since the lock was taken. {safe}"
            )
        return rec

    @staticmethod
    def _recover_target_step(man: Manifest):
        """The unique ``running`` in-flight step `recover` targets (FR-5.6 step 2).

        Aborts (no mutation, no signal) when there is not exactly one — the step
        transitioned concurrently (finished/failed/parked) or the manifest is in a
        shape `recover` must not overwrite.
        """
        running = [s for s in man.steps if s.status == M.RUNNING]
        if len(running) != 1:
            raise RecoverConcurrent(
                f"run {man.run_id!r} has {'no' if not running else 'multiple'} "
                "single in-flight `running` step (the step transitioned "
                "concurrently); no action taken — re-run `gauntlet status`."
            )
        return running[0]

    @staticmethod
    def _identity_still_matches(pid: int, recorded: dict | None) -> bool:
        """True iff ``pid`` is live and its freshly-read identity equals ``recorded``.

        The PID-reuse-safe re-check (FR-5.6 step 5 / reconciliation gate): a
        ``None`` recorded identity, a ``None`` fresh read (dead/reused/unsupported
        platform), or a mismatch all fail closed → not our process → no signal.
        """
        known = ProcessIdentity.from_dict(recorded)
        if known is None:
            return False
        if not _pid_is_live(pid):
            return False
        return known.same_process(read_process_identity(pid))

    def _signal_recover_target(self, intent: "_RecoveryIntent") -> str:
        """Re-verify the frozen intent's identity, then signal the group (FR-5.6 step 5).

        On an exact identity match AND PID-still-in-PGID, signal the recorded
        process group (SIGTERM→SIGKILL). On a mismatch/absent/reused target send
        no signal and report ``already_dead`` — the durable intent already pins
        the verified target, so finalization proceeds without signalling.
        """
        if not self._identity_still_matches(intent.pid, intent.proc_identity):
            return M.SIGNAL_ALREADY_DEAD
        try:
            if os.getpgid(intent.pid) != intent.pgid:
                return M.SIGNAL_ALREADY_DEAD
        except OSError:
            return M.SIGNAL_ALREADY_DEAD
        return _signal_process_group(intent.pgid)

    def _release_lock_if_nonce(self, nonce: str, run_dir: Path | None = None) -> None:
        """Release the drive lock only if it still carries ``nonce`` (FR-5.6 step 8).

        Mirrors :meth:`_release_worktree_lock`'s nonce guard, but keyed on the
        recovered nonce rather than a held handle — `recover` never *acquired* the
        lock, it is releasing the wedged driver's. Never unlinks a new owner's
        fresh lock.

        P7b: the recovered driver published ONE record at BOTH scopes, so both
        are released under the same nonce. Releasing only the per-run lock would
        leave the wedged driver's tree guard behind and wedge every driving verb
        on this worktree — the exact "recoverable, never stuck" line R1 draws.
        Each file is checked independently, so a partially-released pair (a
        crash between the two unlinks) still converges.
        """
        for path in self._lock_paths_for(run_dir):
            current = locking.read_record(path)
            if current is not None and current.nonce == nonce:
                _unlink_durable(path)

    def _lock_paths_for(self, run_dir: Path | None) -> list[Path]:
        """Every path this engine's drive lock can occupy, per-run first (P7b)."""
        paths = [] if run_dir is None else [self._run_lock_path(run_dir)]
        paths.append(self._tree_lock_path())
        return paths

    @staticmethod
    def _guard_run_file(run_dir: Path, name: str) -> Path:
        """Return ``run_dir/name`` after proving it is contained and not a symlink.

        The mutating recover/reconcile paths read and write the manifest (and the
        recovery intent) from bytes ultimately seeded by the active-run pointer.
        Even with the pointer validated, a symlinked or run-dir-escaping target
        file could still redirect a read or write outside the run tree, so refuse
        one fail-closed before any I/O (FR-10.1 / PRD §7 containment, review F-003).
        """
        path = run_dir / name
        if path.is_symlink():
            raise UnsafeRunSegment(f"refusing symlinked run file: {path}")
        try:
            if not _path_within(path.resolve(), run_dir.resolve()):
                raise UnsafeRunSegment(f"run file escapes the run dir: {path}")
        except (OSError, RuntimeError) as exc:
            raise UnsafeRunSegment(f"unresolvable run file {path}: {exc}") from exc
        return path

    @staticmethod
    def _find_step_by_rendered_id(man: Manifest, rendered_id: str):
        """The StepRecord whose rendered id equals ``rendered_id`` (or ``None``).

        Matches by re-rendering (id / id.iteration) rather than parsing, so a
        dotted step id is unambiguous and the lookup agrees with everything else
        that names a leaf (FR-3.1a)."""
        from gauntlet.engine import operator

        for rec in man.steps:
            try:
                if operator.render_step_id(rec) == rendered_id:
                    return rec
            except Exception:  # a corrupt iteration on some other record
                continue
        return None

    def _finalize_recovery(
        self,
        run_dir: Path,
        intent: "_RecoveryIntent",
        outcome: str,
    ) -> bool:
        """FR-5.6 steps 6–8: persist the transition, clear the intent, release the lock.

        Reloads the manifest **fresh from disk** (never a caller's stale in-memory
        copy) and applies the FR-5.6 running-step guard before any write, so a run
        that completed normally in a concurrent window is never overwritten as
        failed/interrupted (review F-002/F-004).

        Idempotent: if a §6.4 record for *this* intent (same ``lock_nonce`` +
        ``prior_step_id``) is already present, step 6 ran on a prior (crashed)
        attempt — skip the manifest write (never a torn or duplicated record) and
        only complete the still-pending steps 7–8. The recovery record is
        therefore written exactly once per recovery, always by whoever finalizes.

        Returns ``True`` when finalized (or already finalized); ``False`` when it
        refuses because the target step is no longer ``running`` — leaving the
        manifest, lock, and intent untouched for the operator to inspect.
        """
        manifest_path = self._guard_run_file(run_dir, "manifest.json")  # F-003
        man = Manifest.load(manifest_path)
        already = any(
            r.lock_nonce == intent.lock_nonce and r.prior_step_id == intent.step_id
            for r in man.recoveries
        )
        if not already:
            rec = self._find_step_by_rendered_id(man, intent.step_id)
            # FR-5.6 running-step guard (review F-002/F-004): only finalize when the
            # target step is STILL running. If it is absent or already terminal the
            # driver completed or the run otherwise transitioned after the intent
            # was written — overwriting it with INTERRUPTED/RUN_FAILED would corrupt
            # a completed run. Refuse WITHOUT touching the manifest, lock, or intent.
            if rec is None or rec.status != M.RUNNING:
                return False
            # Step 6: atomic manifest update — mark the step INTERRUPTED and
            # append the §6.4 record, built from the frozen intent + the observed
            # signal outcome, in a single durable write-temp→fsync→rename→fsync-dir.
            rec.status = M.INTERRUPTED
            # FR-7.2: `gauntlet recover` terminated the step; stamp the disjoint
            # halt_reason (clearing any prior parked_reason) so `status --json`
            # names the cause. The operator identity is on the RecoveryRecord
            # appended below (`actor`/`actor_source`).
            rec.halt_reason = M.HALT_REASON_OPERATOR_RECOVER
            rec.parked_reason = None
            man.status = M.RUN_FAILED
            # #72: recover must leave a *reconcilable* branch/manifest pair. A
            # killed builder can have committed work git knows about but the
            # manifest does not (no flush survived the kill). Always snapshot
            # the killed branch tip behind a backup ref, and record the tip +
            # any unmanifested `last-recorded..tip` commits on the audit record
            # — so the operator's way out is a named, reversible verb
            # (`rollback` absorbs descendants; `resume --reset-interrupted`
            # discards the attempt) instead of forbidden git surgery.
            # Best-effort: a repo-less run tree (or a deleted branch) still
            # finalizes, with the gap recorded as a warning, never silently.
            branch_head, unmanifested = self._record_recovery_reconciliation(
                man, intent
            )
            man.recoveries.append(
                M.RecoveryRecord(
                    ts=intent.ts,
                    actor=intent.actor,
                    actor_source=intent.actor_source,
                    reason=intent.reason,
                    lock_nonce=intent.lock_nonce,
                    pid=intent.pid,
                    pgid=intent.pgid,
                    proc_identity=intent.proc_identity,
                    host=intent.host,
                    signal_outcome=outcome,
                    prior_step_id=intent.step_id,
                    prior_step_status=intent.prior_step_status,
                    prior_run_status=intent.prior_run_status,
                    resulting_step_status=M.INTERRUPTED,
                    resulting_run_status=M.RUN_FAILED,
                    branch_head=branch_head,
                    unmanifested_range=unmanifested,
                )
            )
            man.write_atomic(manifest_path)
            _fsync_dir(run_dir)
        # Step 7: clear the intent only after step 6 is durable — its content is
        # now folded into the persisted record, so a surviving intent always means
        # "manifest not yet finalized".
        _unlink_durable(run_dir / RECOVERY_INTENT_NAME)
        # Step 8: release the lock under the recorded-nonce guard.
        self._release_lock_if_nonce(intent.lock_nonce, run_dir)
        return True

    def _record_recovery_reconciliation(
        self, man: Manifest, intent: "_RecoveryIntent"
    ) -> tuple[str | None, str | None]:
        """Backup ref + branch↔manifest divergence evidence at recover time (#72).

        Writes ``refs/gauntlet/backup/<run_id>/recover-<ts>`` at the run-branch
        tip ALWAYS (any later reconciliation — an absorbing rollback, a
        reset-interrupted resume — is then reversible by construction), and
        returns ``(branch_head, unmanifested_range)`` for the §6.4 record. When
        the tip is strictly ahead of the manifest's last recorded commit, the
        warning names the two sanctioned ways out; a genuine fork is warned
        about too (resume's branch guard will refuse it). Mutates only
        ``man.warnings`` — the caller owns the atomic write.
        """
        try:
            branch_head = gitops.rev_parse(self.repo_root, man.branch)
        except gitops.GitError as exc:
            man.warnings.append(
                f"recover: no backup ref written (branch {man.branch!r} "
                f"unresolvable: {exc}); reconcile branch and manifest manually "
                "before any rewind"
            )
            return None, None
        unmanifested: str | None = None
        # Every git call below is best-effort too (PR #77 review / Copilot):
        # the driver is already dead by the time this runs, so a failing
        # `update-ref`/`log`/observation must degrade to a warning — never
        # escape and leave the manifest unfinalized with the intent stranded.
        try:
            backup = (
                f"refs/gauntlet/backup/{man.run_id}/recover-"
                f"{intent.ts.replace(':', '-')}"
            )
            gitops.create_ref(self.repo_root, backup, branch_head)
            # P4 (plan §4.2/§5.4): the divergence evidence comes from the SAME
            # observation machinery resume reconciles with and status renders
            # — the proven, inventoried branch relation — so recover's warning
            # names exactly what the next resume will do with the range.
            obs_layout = self.layout(man.slug)
            git_obs = self._observe_resume_branch(
                obs_layout, obs_layout.run_dir(man.run_id), man
            )
            relation = git_obs.branch_relation
            boundary = git_obs.recorded_sha
            if relation in RX.ADOPTABLE_AHEAD_RELATIONS and boundary is not None:
                unmanifested = gitops.log_range(
                    self.repo_root, boundary, branch_head
                )
                n = len(git_obs.run_branch_commits)
                man.warnings.append(
                    f"recover: branch {man.branch!r} is {n} commit(s) "
                    f"ahead of the recorded boundary {boundary[:10]} "
                    f"(relation {relation.value}; the driver was killed "
                    f"before a manifest flush); killed state backed up at "
                    f"{backup}. A plain `gauntlet resume {man.slug}` adopts "
                    "the range into the manifest (plan §5.4/R6); "
                    f"`gauntlet rollback {man.slug} --phase N` absorbs it "
                    f"after snapshot, and `gauntlet resume {man.slug} "
                    "--reset-interrupted` discards the interrupted attempt "
                    "(checkpoint-preserving)."
                )
            elif relation in (
                RX.BranchRelation.BEHIND,
                RX.BranchRelation.FORKED,
            ):
                man.warnings.append(
                    f"recover: branch {man.branch!r} tip "
                    f"{branch_head[:10]} has diverged from the recorded "
                    f"boundary {boundary[:10] if boundary else '?'} "
                    f"(relation {relation.value}); killed state backed up at "
                    f"{backup}. Resume/rollback will refuse until the branch "
                    "is restored."
                    + self._relation_action_detail(git_obs, man)
                )
        except (gitops.GitError, RX.RecoveryExecError) as exc:
            man.warnings.append(
                f"recover: branch↔manifest reconciliation incomplete (git "
                f"failed after resolving {man.branch!r} at {branch_head[:10]}: "
                f"{exc}); verify refs/gauntlet/backup/ before any rewind"
            )
            return branch_head, None
        return branch_head, unmanifested

    def _reconcile_projection(self, run_dir: Path, slug: str) -> None:
        """P6: reconcile the manifest projection with the journal (plan §4.6).

        Runs at the start of every mutating verb, before the manifest is
        loaded or the FR-5.6 intent reconciliation reads it — the journal is
        the authoritative state (R8), and ``manifest.json`` is its projection:

        * a pre-P6 run (no journal) gets its deterministic genesis event from
          a LOADABLE manifest (plan §8) and otherwise behaves exactly as
          before;
        * a projection behind the journal head (a kill between event append
          and projection write, or a branch reset that materialized an old
          committed manifest — R8) is caught up idempotently, loudly;
        * a projection the journal has never recorded (an out-of-band write)
          is preserved as durable evidence and replaced from the head, loudly
          — never silently adopted as authority (post-P6 review F-001);
        * a missing/corrupt projection is rebuilt from the journal head
          through the shared executor action (plan §5.5): the SAME
          :func:`RX.projection_rebuild_assessment` the read-only status
          surface renders (R4), applied under the executor's ordering with
          the malformed original preserved as evidence first.

        Ordering (post-P6 review F-003): the check is READ-ONLY first, and any
        WRITE happens under the worktree lock — observe → lock → re-observe →
        apply, the same discipline the recovery executor uses. A lock held by
        a **live** driver means the divergence is that driver's own in-flight
        transition (the event landed, its projection write is microseconds
        away): skip silently rather than race it or refuse the verb — whichever
        verb genuinely needs the lock still fails closed on its own terms.

        Read-only ``status`` never calls this — it detects and reports via
        :func:`operator.load_projection_view`.
        """
        # Read-only pre-check: a healthy projection is the overwhelmingly
        # common case and must cost no lock and no write.
        pre = J.projection_status(
            run_dir, mutate=False, validate=M.validate_projection_text
        )
        genesis_pending = (
            pre.health == J.HEALTH_NO_JOURNAL
            and (run_dir / "manifest.json").exists()
        )
        if pre.health == J.HEALTH_OK or (
            pre.health == J.HEALTH_NO_JOURNAL and not genesis_pending
        ):
            return
        try:
            handle = self._acquire_worktree_lock(slug, pre.run_id, run_dir=run_dir)
        except WorktreeLockError:
            # A live driver owns the projection; its own write lands next.
            return
        try:
            outcome = J.reconcile_projection(
                run_dir, validate=M.validate_projection_text
            )
            if outcome.rebuild_required:
                planned = RX.projection_rebuild_assessment(
                    self.repo_root, run_dir, slug=slug
                )
                if planned is None:
                    raise J.JournalError(
                        f"manifest projection under {run_dir} is "
                        "missing/corrupt and no rebuild action could be "
                        "planned; inspect the journal dir before retrying"
                    )
                assessment, action = planned
                # The executor's own guard verifies (never reclaims) the
                # lock — reclaim policy with its identity verification
                # belongs here; the guard sees the lock as this process's.
                RX.RecoveryExecutor(
                    self.repo_root,
                    run_dir,
                    run_id=outcome.run_id or "unknown",
                    run_root=self.config.run_root,
                ).apply_rebuild(assessment, action)
                return
            if outcome.health in (J.HEALTH_CAUGHT_UP, J.HEALTH_RESTORED):
                # Loud, durable audit: the reconciliation notes land as
                # manifest warnings through the normal journaled persist (a
                # fresh transition — so the R5 fingerprint provably moves).
                man = Manifest.load(run_dir / "manifest.json")
                changed = False
                for note in outcome.notes:
                    warn = f"[projection] {note}"
                    if warn not in man.warnings:
                        man.warnings.append(warn)
                        changed = True
                if changed:
                    man.write_atomic(run_dir / "manifest.json")
        finally:
            self._release_worktree_lock(handle)

    def _reconcile_projection_safe(self, layout: "RunLayout") -> None:
        """Reconcile the projection when a run dir resolves; else skip (P6).

        For a cleanup verb that legitimately runs after the active-run pointer
        is gone or stale: no run dir means there is no projection to
        reconcile, which is not an error for that verb.
        """
        try:
            run_dir = layout.active_run_dir()
        except (FileNotFoundError, UnsafeRunSegment):
            return
        self._reconcile_projection(run_dir, layout.slug)

    def _reconcile_recovery_intent(self, run_dir: Path) -> str | None:
        """Finalize or discard a surviving recovery intent (FR-5.6, mutating).

        Runs at the start of every `recover` and on the `resume` path (both
        already mutating). Read-only `status` never calls this — it only
        *detects and reports* via :func:`operator.read_recovery_intent`.

        Keyed on the intent, **not** a fresh liveness gate (a now-dead target is
        the *expected* post-signal outcome, not a failure):

        * **Malformed** — lock present but unreadable/unparseable: mutate
          nothing, keep the intent, surface the file (review F-002). "Cannot
          read the lock" is never "the lock is absent".
        * **Stale** — lock **present** with a **different** nonce (a relaunched
          driver holds a fresh lock): discard the intent, no signal, no manifest
          mutation.
        * **Live** — lock **absent** (verified target already killed, nothing
          relaunched) **or** present with a matching nonce: finalize idempotently.
          Re-run the FR-5.1 identity gate against the frozen intent — only an
          exact match may (re-)signal (a no-op SIGKILL); a mismatch/absent target
          sends no signal and records ``already_dead`` — then perform steps 6–8.

        Returns a short human note describing what was done, or ``None`` when no
        intent survives. A malformed/unreadable intent is left untouched (fail
        closed: no trustworthy facts to act on) and surfaced for the operator.
        """
        intent_path = run_dir / RECOVERY_INTENT_NAME
        # Containment (review F-003): the intent drives process signalling and a
        # manifest mutation, so — like the read-only parser — refuse a symlinked or
        # run-dir-escaping path with NO read. In this mutating context we leave it
        # untouched for the operator rather than act on attacker-redirected bytes.
        if intent_path.is_symlink():
            return (
                "symlinked recovery intent present; left in place for inspection "
                "(refusing to follow it out of the run dir)."
            )
        if not intent_path.exists():
            return None
        try:
            if not _path_within(intent_path.resolve(), run_dir.resolve()):
                return (
                    "recovery intent path escapes the run dir; left in place for "
                    "inspection."
                )
        except (OSError, RuntimeError):
            return "unresolvable recovery intent present; left in place for inspection"
        try:
            text = intent_path.read_text()
        except OSError:
            return "unreadable recovery intent present; left in place for inspection"
        intent = _RecoveryIntent.from_json(text)
        if intent is None:
            return "malformed recovery intent present; left in place for inspection"

        kind, current = self._read_lock_state(run_dir)
        if kind == locking.LOCK_MALFORMED:
            # The lock EXISTS but could not be read or parsed (review F-002,
            # confirm pass). This is not the "absent" case the live branch below
            # is written for: absent means "the verified target was killed and
            # nothing relaunched", which is a fact. Unreadable is the absence of
            # a fact, and finalizing on it would signal a process and rewrite the
            # manifest on evidence we never saw. `operator.read_recovery_intent`
            # already fails closed here (`nonce_matches = False`), so treating it
            # as absent was also a direct R4 disagreement between the read-only
            # view and this mutating path. Mutate nothing; leave the intent for
            # the operator, who is told exactly which file to look at.
            return (
                "recovery intent present, but the drive lock "
                f"({self._run_lock_path(run_dir) if run_dir else self._tree_lock_path()}) "
                "exists and cannot be read or parsed; refusing to finalize a "
                "recovery against a lock whose holder cannot be identified "
                "(fail closed). Left in place — inspect the lock, and remove it "
                "by hand once you have confirmed no driver is running."
            )
        if current is not None and current.nonce != intent.lock_nonce:
            # Stale: a relaunched driver holds a fresh lock → discard, no signal,
            # no manifest mutation.
            _unlink_durable(intent_path)
            return (
                "stale recovery intent discarded; run relaunched — re-run "
                "`gauntlet status`."
            )

        # Live branch: finalize — but only when the target step is still `running`
        # (review F-002). A non-running target means the driver completed or the run
        # otherwise transitioned after the intent was written: refuse WITHOUT
        # signalling, mutating the manifest, or deleting the intent. The exception
        # is an already-recorded recovery (step 6 ran on a prior crashed attempt,
        # so the step is already INTERRUPTED): there only steps 7–8 remain, which
        # `_finalize_recovery` completes idempotently.
        man = Manifest.load(self._guard_run_file(run_dir, "manifest.json"))  # F-003
        already = any(
            r.lock_nonce == intent.lock_nonce and r.prior_step_id == intent.step_id
            for r in man.recoveries
        )
        if not already:
            target = self._find_step_by_rendered_id(man, intent.step_id)
            if target is None or target.status != M.RUNNING:
                return (
                    "recovery intent present but its target step is no longer "
                    "running (the run transitioned concurrently); left in place for "
                    "inspection — re-run `gauntlet status`."
                )
        # Compute the (re-)signal outcome against the frozen intent — already_dead
        # is the expected case post-crash. A verified-but-unsignalable driver
        # (EPERM) is still alive: clear the intent so reconciliation does not retry
        # the un-killable signal on every later entry point, leave the manifest
        # running for manual intervention, and surface the note (review F-005).
        try:
            outcome = self._signal_recover_target(intent)
        except RecoverSignalError as exc:
            _unlink_durable(intent_path)
            return f"could not finalize recovery: {exc} (intent cleared)."
        if not self._finalize_recovery(run_dir, intent, outcome):
            # The target transitioned out of `running` between the pre-check and
            # the finalize reload (a tight race); finalize refused without mutating.
            return (
                "recovery intent present but its target step is no longer running "
                "(the run transitioned concurrently); left in place for inspection "
                "— re-run `gauntlet status`."
            )
        return "finalized an interrupted recovery from its surviving intent."

    # ---- clean (run-branch tidy) --------------------------------------------
    def clean(self, slug: str, *, force: bool = False) -> str:
        """Delete the run branch once it is merged; preserve the run record.

        Safe by construction: refuse unless ``gauntlet/<slug>`` is fully merged
        into its recorded base (``--force`` overrides). Removes only the
        ephemeral branch + the live ``active-run.txt`` pointer — never the
        committed run dir (prd.md, manifest, transcripts are the audit trail).
        """
        layout = self.layout(slug)
        # OPERATOR tree by design: `clean` deletes the run branch, and what
        # can block that is the human standing on it (P7a, spike §9.4).
        repo = self.operator_root
        # FR-6: reap an orphaned judge while the active-run pointer still
        # resolves the run dir (clean clears it below). Driver-gone-only +
        # identity-verified; a live run's judge and the shared console are left
        # untouched.
        self._reap_orphaned_judge_safe(layout)
        # P6 (post-review F-003): clean deletes the run branch off the run's
        # recorded base, so reconcile the projection with the authoritative
        # journal first. Safe-wrapped: `clean` legitimately runs when the
        # pointer is already stale and there is no run dir to reconcile.
        self._reconcile_projection_safe(layout)
        # Review F-005: `clean` destroys a run's tree AND its branch, and until
        # now it acquired no drive lock at all — so it could pull the working
        # directory out from under a live driver and delete the branch that
        # driver is committing to. The worktree-global tree guard never covered
        # this (clean never took it either), so it is a gap P7c opened when it
        # gave `clean` a worktree to release, not one the guard used to close.
        #
        # Liveness is read BEFORE the lock is taken, for the reason P7c-1.1's
        # F-003 recorded: a verb holding the drive lock looks like a live driver
        # to anything that consults liveness, so acquiring first would make
        # `clean` refuse itself. `indeterminate` fails closed with `alive` — the
        # same asymmetry `recover`, `_reap_orphaned_judge` and the migration
        # gate take, and the safe direction when the alternative is destroying a
        # tree we cannot prove is idle.
        self._refuse_clean_under_a_live_driver(slug, layout)
        branch = f"{self.config.branch_prefix}{slug}"
        if not gitops.branch_exists(repo, branch):
            cleared = self._clear_active_pointer(layout)
            return (
                f"no branch {branch!r}"
                + ("; cleared stale active-run pointer" if cleared else "; nothing to do")
            )
        handle = self._acquire_worktree_lock(slug, None, run_dir=self._clean_run_dir(layout))
        try:
            return self._clean_locked(slug, layout, repo, branch, force=force)
        finally:
            self._release_worktree_lock(handle)

    @staticmethod
    def _clean_run_dir(layout: "RunLayout") -> Path | None:
        """The run-instance dir for `clean` to lock, when one resolves.

        ``clean`` legitimately runs with a stale pointer and no run dir — spike
        §11 row 3, "a stale worktree whose run is gone", the case where the tree
        most needs removing. There is no per-run lock to take then, and the tree
        guard alone is the right (and only) exclusion.
        """
        try:
            return layout.active_run_dir()
        except (OSError, ValueError):  # FileNotFoundError is an OSError
            return None

    def _refuse_clean_under_a_live_driver(self, slug: str, layout: "RunLayout") -> None:
        """Fail closed rather than tear a tree away from a driver (review F-005)."""
        run_dir = self._clean_run_dir(layout)
        if run_dir is None:
            return  # no run instance → nothing is driving it
        try:
            liveness = self._migration_liveness(slug, run_dir)
        except (OSError, ValueError):
            return  # cannot read a driver record at all → treat as no driver
        if liveness in self._migratable_liveness():
            return
        from gauntlet.engine import operator

        detail = (
            "a driver is LIVE and is driving this run right now"
            if liveness == operator.LIVENESS_ALIVE
            else "this run's driver state is INDETERMINATE — its lock could not "
            "be read, so it may still be running"
        )
        raise WorktreeLockError(
            f"refusing clean for {slug!r}: {detail}. `clean` removes the run's "
            "worktree and deletes its branch, and doing that under a driver "
            "takes the working directory out from under it mid-step.\n"
            f"  Stop it first (`gauntlet abort {slug}`), or wait for it to "
            f"finish; `gauntlet status {slug}` shows the driver. If you are "
            "sure the driver is dead, `gauntlet recover` proves it and clears "
            "the lock."
        )

    def _clean_locked(
        self,
        slug: str,
        layout: "RunLayout",
        operator_root: Path,
        branch: str,
        *,
        force: bool,
    ) -> str:
        """:meth:`clean`'s body, with the drive lock held.

        The tree parameter is named ``operator_root``, not ``repo``: `clean`
        steps off the run branch and deletes it FROM THE HUMAN'S CHECKOUT by
        design (spike §9.4), and `test_root_scope` bans the ambiguous name from
        every work-scoped call precisely so that set stays greppable.
        """
        # PROBLEM D / spike E2-D: with a live worktree on this branch,
        # `branch -D` hard-refuses ("cannot delete branch ... used by worktree
        # at ..."). The tree must be unlocked and removed FIRST, and a dirty
        # one is snapshotted before any `--force` (§11 row 10 / R2). A no-op
        # for a `same_tree` run and for a tree that is already gone.
        self._release_run_worktree_for_slug(layout)
        base = self._recorded_base(layout)
        if not force:
            if base is None:
                raise RunBranchNotMergedError(
                    f"cannot determine the base for {branch!r} (no run manifest); "
                    "merge it and retry, or pass --force to delete anyway"
                )
            if not gitops.is_ancestor(operator_root, branch, base):
                raise RunBranchNotMergedError(
                    f"refusing to delete {branch!r}: not fully merged into base "
                    f"{base!r}. Merge it first (e.g. `gauntlet finish {slug}`), "
                    "or pass --force to discard it."
                )
        if gitops.current_branch(operator_root) == branch:
            target = base
            if target is None or target == branch:
                raise RunBranchNotMergedError(
                    f"on {branch!r} with no recorded base to step onto; check "
                    "out another branch first, then `gauntlet clean`"
                )
            # F-2: stepping off the branch with a dirty tree would carry the
            # uncommitted changes onto the base (or fail mid-checkout). Refuse.
            # Exclude only the run-instance BOOKKEEPING (manifest/transcripts/
            # PR.md) — NOT the whole run root, which would hide tracked artifacts
            # like prd.md/plan.md and let their uncommitted edits ride onto base.
            excludes = run_bookkeeping_excludes(
                operator_root, layout.active_run_dir(), layout.slug_dir
            )
            if not gitops.is_clean(operator_root, exclude=excludes):
                raise WorktreeDirtyError(
                    f"refusing clean: worktree is dirty and clean must step off "
                    f"{branch!r} onto {target!r}, which would carry the changes "
                    "onto the base. Commit or discard them first."
                )
            gitops.checkout_branch(operator_root, target)
        gitops.delete_branch(operator_root, branch)
        self._clear_active_pointer(layout)
        return f"deleted {branch!r}" + (" (forced)" if force else "")

    # ---- finish (merge into base + tidy) ------------------------------------
    def finish(self, slug: str) -> str:
        """Merge a completed run into its base, then clean up (one-verb land).

        Fail closed: requires the run to be ``done`` and the worktree clean,
        then merges ``gauntlet/<slug>`` into its recorded base with a merge
        commit, deletes the branch, and clears the active pointer. A merge
        conflict is aborted (never left half-applied) and surfaced for a manual
        merge. Wraps :meth:`clean`'s cleanup; ``clean`` stays the primitive for
        teams whose gauntlet->base merge is itself a reviewed PR.
        """
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        # F-003 / PROBLEM A: `finish` checks out the base and merges IN THE
        # OPERATOR'S CHECKOUT. That is the precise vector the retained
        # worktree-global tree guard exists to close, and `finish` was the one
        # verb that never took it — so a `dedicated` run's finish could swap the
        # branch under a live `same_tree` run of another slug. Keeping the guard
        # only closes the vector if every operator-tree mutation holds it.
        #
        # Acquired around the WHOLE verb (not just the merge): the checkout, the
        # merge, the branch delete and the pointer clear are one transaction
        # from the other run's point of view.
        # P6 (post-review F-003): finish merges and DELETES a branch off the
        # run's recorded status, so it must read the authoritative state, not
        # a projection left stale by a branch reset or a kill window —
        # otherwise it could refuse a journal-complete run, or merge and
        # delete on the word of an older projection that still says `done`.
        #
        # BOTH of the steps below consult DRIVER LIVENESS, and `finish` is
        # about to hold the drive lock itself — which would make finish look
        # like that live driver to its own sub-operations. So both run BEFORE
        # the acquisition (this is also why `_resume_once` reconciles first):
        #
        # * `_reconcile_projection` treats an un-acquirable drive lock as "a
        #   live driver owns the projection" and returns without reconciling,
        #   so finish would act on the stale projection it exists to distrust;
        # * `_reap_orphaned_judge` is driver-gone-only, so it would decline to
        #   reap and leak the run's judge process.
        #
        # Both are safe here: the reconcile is journal-authoritative and the
        # reap is identity-verified and driver-gone-only, neither depends on
        # holding the guard.
        self._reconcile_projection(run_dir, slug)
        # FR-6: a completed run's driver is gone, so reap its orphaned judge
        # (identity-verified, driver-gone-only); never the shared console.
        self._reap_orphaned_judge(run_dir, slug)
        finish_handle = self._acquire_worktree_lock(slug, None, run_dir=None)
        try:
            return self._finish_locked(slug, layout, run_dir)
        finally:
            self._release_worktree_lock(finish_handle)

    def _finish_locked(self, slug: str, layout: "RunLayout", run_dir: Path) -> str:
        """:meth:`finish`'s body, with the worktree-global guard held."""
        man = Manifest.load(run_dir / "manifest.json")
        # OPERATOR tree by design: `finish` merges the run branch INTO the
        # operator's base and leaves the human on a sensible branch — the
        # one verb whose whole purpose is to touch their checkout (P7a).
        repo = self.operator_root
        branch, base = man.branch, man.base_branch

        if man.status != M.RUN_DONE:
            raise FinishError(
                f"run {man.run_id!r} is {man.status!r}, not done; finish merges "
                "only a completed run — resume or approve its gates first"
            )
        excludes = run_bookkeeping_excludes(self.repo_root, run_dir, layout.slug_dir)
        if self._effective_worktree_mode(man) == WT.MODE_DEDICATED:
            # Under `dedicated` the governed artifacts legitimately sit
            # UNCOMMITTED in the operator's checkout forever: that checkout is
            # the authoring surface (§14.2 option A), and what reaches the run
            # branch is the copy the sync publishes into the run worktree. So
            # `runs/<slug>/prd.md` here is the human's source file, not dirt,
            # and blocking finish on it would make finish impossible for every
            # dedicated run.
            excludes = excludes + governed_artifact_paths(
                self.operator_root, layout.slug_dir
            )
        if not gitops.is_clean(repo, exclude=excludes):
            dirt = gitops.status_porcelain(repo, exclude=excludes, untracked_all=True)
            listing = "\n  ".join(dirt.splitlines()[:8])
            raise FinishError(
                "refusing finish: worktree is dirty; commit or discard first.\n"
                f"  Tree inspected: {repo}\n  {listing}\n"
                f"  Inspect it with: git -C {repo} status"
            )
        if not gitops.branch_exists(repo, branch):
            raise FinishError(f"run branch {branch!r} does not exist")
        if not gitops.branch_exists(repo, base):
            raise FinishError(f"base branch {base!r} does not exist")
        # F-011: the cleanliness guard above inspects the OPERATOR's checkout,
        # which is correct for the merge but says nothing about the run's own
        # tree — and the release below force-removes that tree. Without this,
        # `finish` would silently convert a builder's uncommitted work into a
        # recovery ref the operator never asked for and would not know to look
        # for. Refuse instead, naming the tree and how to inspect it.
        # P7d (a P7c-2 deferral, weighed and taken): the RUN tree's check gets
        # the same exclusion set every other run-tree surface uses, instead of
        # none at all. Unexcluded, it counted the engine's own write-only export
        # and the synced governed artifacts as "a builder's uncommitted work" —
        # so a dedicated run that reached `done` with an untracked synced
        # `prd.md` in its tree was refused by the guard that exists to protect
        # something else entirely. It was strictly *stricter* than finish's own
        # operator-tree check three lines up, which has excluded exactly this
        # set since P7c-1.1, and the asymmetry was an oversight rather than a
        # decision. Governed artifacts still earn their exclusion by BYTE
        # COMPARISON (review F-003) — an artifact edited inside the run tree is
        # divergent, is not excluded, and still blocks.
        run_excludes = self._run_tree_excludes(layout, man, run_dir)
        self._refuse_if_run_worktree_dirty(
            man, verb="finish", excludes=run_excludes
        )
        # Ordered AFTER the dirt refusal on purpose: uncommitted work that a
        # teardown would sweep into a recovery ref is a bigger deal than a merge
        # that cannot start, so the operator hears about it first.
        # Under `dedicated` the operator's checkout holds the governed artifacts
        # UNTRACKED (they are the authoring surface; the run branch commits the
        # synced copy). The merge below wants to write those same paths as
        # TRACKED files, and git refuses outright when an untracked working-tree
        # file would be overwritten — before starting the merge, so there is
        # nothing to abort.
        #
        # P7d splits what P7c-1.1 refused wholesale. A DIVERGENT duplicate still
        # refuses here, early, having destroyed nothing. An IDENTICAL one is
        # SET ASIDE (moved, never deleted) just before the checkout+merge that
        # restores it — see `_quarantine_identical_merge_collisions`.
        #
        # Review F-003: the collision set covers the base ref as well as the run
        # branch, because `checkout_branch(repo, base)` below refuses on the
        # same untracked-overwrite rule the merge does — and it runs AFTER the
        # run worktree has been released, so a refusal there is not retryable.
        self._refuse_on_untracked_merge_collision(layout, man, repo, base)
        # PROBLEM D / spike E2-D: release the run worktree BEFORE the branch is
        # merged and deleted. The merge itself is unaffected by a live worktree
        # (E2-F/G measured that, and it leaves the operator exactly where they
        # were), but the `branch -D` that follows is not. Ordering, not taste.
        #
        # The release takes the RUN tree's excludes for the same reason the
        # refusal above does: this list also feeds `WT.release`'s snapshot
        # decision, and the operator-derived set answered that question about
        # the wrong tree. Empty is safe rather than a fallback case — the two
        # go empty under exactly the same conditions (no tree, an unreadable
        # worktree list, or a registered-but-absent tree), and in every one of
        # them `_release_run_worktree` returns early or `WT.release` finds no
        # directory to inspect, so the list is never consulted.
        self._release_run_worktree(run_dir, man, excludes=run_excludes)

        # Set aside BEFORE any checkout, not just before the merge: BOTH write
        # these paths and both refuse on an untracked file (review F-003), and
        # both run AFTER the release above — so a refusal there is not fixable
        # by retrying. Hoisted above the already-merged branch so the two
        # landing paths cannot diverge on this; reasoning that one of them
        # "cannot reach a collision" is exactly the kind of argument that stops
        # being true when the surrounding code moves.
        moved = self._quarantine_identical_merge_collisions(layout, man, repo, base)
        cleared = [rel for rel, _held in moved]

        # Already merged (e.g. landed via a PR): nothing to merge, just tidy.
        if gitops.is_ancestor(repo, branch, base):
            try:
                if gitops.current_branch(repo) == branch:
                    gitops.checkout_branch(repo, base)
            except gitops.GitError as exc:
                self._settle_quarantined(moved)
                raise FinishError(
                    f"{branch!r} is already merged into {base!r} but the base "
                    f"could not be checked out: {exc}\n"
                    "  The branch was NOT deleted. Nothing else was changed."
                )
            # Settle FIRST, then report what settling actually did. On this path
            # a checkout may not have run at all (the operator was already off
            # the run branch), in which case nothing restored the artifact and
            # `_settle_quarantined` simply put it back — so claiming a
            # replacement here would describe an operation that did not happen.
            # `restored` is the paths whose set-aside copy is still on disk.
            restored = self._settle_quarantined(moved)
            gitops.delete_branch(repo, branch)
            self._clear_active_pointer(layout)
            note = ""
            if cleared:
                note = (
                    f"; your untracked {', '.join(cleared)} was set aside for "
                    "the landing and is back in place"
                )
            if restored:
                note += (
                    f"; a differing version was already present, so your copy "
                    f"was left beside it as {', '.join(restored)}"
                )
            return f"already merged into {base!r}; deleted {branch!r}{note}"

        try:
            gitops.checkout_branch(repo, base)
        except gitops.GitError as exc:
            self._settle_quarantined(moved)
            raise FinishError(
                f"could not check out base {base!r} to land {branch!r}: {exc}\n"
                "  Nothing was merged. The run worktree has already been "
                "released, so re-running `gauntlet finish` is safe once the "
                "base is checkable-out."
            )
        msg = f"Merge {branch} into {base} (gauntlet finish {slug}, run {man.run_id})"
        try:
            gitops.merge_branch(repo, branch, message=msg)
        except gitops.GitError as exc:
            # git can refuse a merge BEFORE starting one (an untracked file that
            # would be overwritten, an unreadable ref). There is no MERGE_HEAD
            # then, so `merge --abort` itself fails — and an exception from the
            # CLEANUP path would replace the real cause with a confusing
            # "no merge to abort". Cleanup is best-effort; the original error is
            # what the operator needs.
            try:
                gitops.merge_abort(repo)
            except gitops.GitError:
                pass
            try:
                gitops.checkout_branch(repo, branch)  # leave the human where they were
            except gitops.GitError:
                pass
            kept = self._settle_quarantined(moved)
            restored = ""
            if cleared:
                restored = (
                    f"\nYour untracked {', '.join(cleared)} was set aside for "
                    "the landing and has been put back."
                )
            if kept:
                restored += (
                    f"\n  NOTE: git had already restored a DIFFERENT version at "
                    f"that path, so your copy was left beside it as "
                    f"{', '.join(kept)} rather than overwriting either. Compare "
                    "them and keep the one you want."
                )
            raise FinishError(
                f"merge of {branch!r} into {base!r} conflicts (or was refused "
                f"outright); resolve it manually — any half-merge was aborted "
                f"and you are back on {branch!r}. Details: {exc}{restored}"
            )
        # The merge landed, so git has rewritten every quarantined path as a
        # tracked file. Only now is the set-aside copy redundant.
        self._settle_quarantined(moved)
        gitops.delete_branch(repo, branch)
        self._clear_active_pointer(layout)
        note = (
            f"; replaced your untracked duplicate(s) of {', '.join(cleared)} "
            "with the merged tracked copy (same bytes and file mode)"
            if cleared
            else ""
        )
        return f"merged {branch!r} into {base!r} and deleted the branch{note}"

    def _recorded_base(self, layout: "RunLayout") -> str | None:
        """The resolved base branch recorded by the run, or None if unreadable."""
        try:
            man = Manifest.load(layout.active_run_dir() / "manifest.json")
        except (OSError, ValueError, FileNotFoundError):
            return None
        return man.base_branch

    @staticmethod
    def _clear_active_pointer(layout: "RunLayout") -> bool:
        """Remove the live active-run pointer (gitignored bookkeeping). Idempotent."""
        if layout.active_pointer.exists():
            layout.active_pointer.unlink()
            return True
        return False

    # ---- migrate-worktree (spike §10) ---------------------------------------
    @staticmethod
    def _refuse_migration_in_pipeline_context(verb: str) -> None:
        """Operator-only, on the same boundary and for the same reason as `recover`.

        ``GAUNTLET_STEP_ID`` is the per-step marker the orchestrator exports to
        every in-run agent. A pipeline agent shelling out to this verb would be
        refused for its OWN run anyway (its driver is alive), but nothing stops
        it reaching another slug's — and relocating the tree another run is
        driving in is not a thing any builder or reviewer has business doing.
        Refused before any read, reconcile or mutation, so the refusal cannot
        be half-applied.
        """
        if os.environ.get("GAUNTLET_STEP_ID"):
            raise MigrateWorktreeRefused(
                f"refusing `{verb}` inside a pipeline-agent context "
                "(GAUNTLET_STEP_ID is set): moving a run's worktree is an "
                "operator-only action, never an in-pipeline step. Nothing was "
                "read or modified. Run it from an operator session instead."
            )

    def _migration_liveness(self, slug: str, run_dir: Path) -> str:
        """This run's driver liveness, read through the one sanctioned primitive.

        Called BEFORE the drive lock is acquired, always. P7c-1.1's F-003 fix
        recorded why: a verb that holds the drive lock looks like a LIVE DRIVER
        to anything that consults liveness, so a migration that acquired first
        and asked second would refuse ITSELF — and `finish` / `_resume_once`
        already order it this way for exactly that reason.
        """
        from gauntlet.engine import operator

        return operator.driver_liveness(
            self._run_root_dir(), slug, run_instance_dir=run_dir
        )

    def migrate_worktree(self, slug: str) -> str:
        """Move a `same_tree` run into its own worktree — explicitly (spike §10).

        The migration ACTION, the second half of the seam
        (`proposals/P7c-split-seam.md` §2). P7c-1 shipped the DECISION — a
        pre-P7c run keeps driving `same_tree` and nothing may move it
        implicitly — and this is the only thing in the engine that may move it,
        only when a human asks by name.

        **Copy, never move, and journalled.** Spike §10's six steps in order:

        1. take the per-run drive lock — after resolving liveness (above);
        2. ``worktree add`` at the derived path. Git refuses if the branch is
           checked out anywhere (E2-A), which in `same_tree` mode is the NORMAL
           state — the run's own branch is in the operator's checkout. That
           refusal is the correct answer, not a bug to route around: this verb
           never checks out, resets or moves a branch in the operator's tree
           (that is the whole of what P7 is protecting), so it hands the
           operator git's own message plus the one action that clears it;
        3. ``git worktree lock --reason`` — done by :func:`WT.ensure`, which is
           also where the fail-closed unwind lives;
        4. write the two-file export dir and VERIFY the bookkeeping paths
           resolve in the new tree; a failure here removes the worktree again;
        5. append ``WorktreeAdopted``;
        6. leave the operator's checkout untouched — read only.

        **A failure at any step leaves the run exactly as it was**, still
        driving `same_tree`, still resumable. Nothing here writes
        ``Manifest.worktree_mode``: a migrated run is `dedicated` because the
        EVIDENCE says so (resolver rules 1 and 2), which is what lets
        :meth:`rollback_worktree_migration` put it back exactly. Recording the
        mode would make the rollback a lie — rule 3 would keep answering
        `dedicated` with no tree in sight.

        Governed artifacts are deliberately NOT synced here. `_run_paths` syncs
        them on the first drive after migration, as it does for every dedicated
        run, and doing it now would leave the freshly-created tree DIRTY with no
        commit behind it — breaking the clean-tree invariant on a tree no agent
        has touched yet. See the commit body for the `finish` collision this run
        inherits as a result (P7c-1.1's second surfaced defect); migration
        neither creates nor smooths it, and reports it in the success message.
        """
        safe_run_segment(slug, kind="slug")
        self._refuse_migration_in_pipeline_context("gauntlet migrate-worktree")
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        # BOTH of the steps below consult driver liveness, and this verb is
        # about to hold the drive lock itself — see `_migration_liveness` and
        # `finish`'s identical ordering note. The reconcile is
        # journal-authoritative (R8) and matters here because the terminal
        # check reads `man.status`: migrating on the word of a projection left
        # stale by a kill window is exactly the class of decision P6 moved off
        # the projection.
        self._reconcile_projection(run_dir, slug)
        man = Manifest.load(run_dir / "manifest.json")
        liveness = self._migration_liveness(slug, run_dir)
        blocker = self.migration_blocker(man, liveness=liveness)
        if blocker is not None:
            raise MigrateWorktreeRefused(self._still_resumable(blocker, man))
        handle = self._acquire_worktree_lock(slug, man.run_id, run_dir=run_dir)
        try:
            # Re-read under the lock. Between the check above and the
            # acquisition another engine could have migrated this run; the
            # liveness leg is deliberately NOT re-checked, because we now hold
            # the lock that makes it read `alive` — us.
            man = Manifest.load(run_dir / "manifest.json")
            blocker = self.migration_blocker(man, liveness=liveness)
            if blocker is not None:
                raise MigrateWorktreeRefused(self._still_resumable(blocker, man))
            return self._migrate_locked(layout, run_dir, man)
        finally:
            self._release_worktree_lock(handle)

    def _still_resumable(
        self, blocker: str, man: Manifest, *, mode: str | None = None
    ) -> str:
        """Append the R1 clause every §10 refusal owes the operator.

        Spike §10's last table row is not advice, it is the obligation: "stays
        fully resumable in `same_tree` mode; the refusal names the blocker …
        the run is never wedged by the migration being impossible." A refusal
        that stops at the blocker satisfies half of that row, so the clause is
        appended here rather than repeated (and eventually forgotten) at each
        raise site.

        **The clause is mode-aware** (review F-007). A rollback refusal is
        evaluated only after the resolver has already answered `dedicated`, so
        telling that operator their run "remains fully drivable in `same_tree`
        mode" is not a harmless simplification — it is a false statement about
        which tree their agents will edit next, handed to them at the moment
        they are trying to work out what state they are in. ``mode`` names the
        mode this refusal LEAVES the run in; it is observed by the caller, never
        assumed here.
        """
        if mode == WT.MODE_DEDICATED:
            where = "keeps driving its own dedicated worktree"
            try:
                state = WT.describe(
                    self.operator_root, mode=mode, branch=man.branch
                )
                if state.path is not None:
                    where = f"keeps driving its own worktree at {state.path}"
            except Exception:
                pass
            return (
                f"{blocker}\n"
                f"Nothing was moved or modified. The run is untouched and "
                f"{where}: `gauntlet resume {man.slug}` works exactly as it did "
                "before you ran this."
            )
        return (
            f"{blocker}\n"
            "Nothing was moved or modified. The run is untouched and remains "
            f"fully drivable in `same_tree` mode: `gauntlet resume {man.slug}` "
            "works exactly as it did before you ran this."
        )

    def _relocate_legacy_worktree(
        self, layout: "RunLayout", run_dir: Path, man: Manifest, common: Path
    ) -> Path | None:
        """Free a pre-P7e tree so the run can be re-created at the new root (P7e).

        The second thing `migrate-worktree` now does. A run that opted into
        `dedicated` under a P7c/P7d engine has its tree at
        ``<git-common-dir>/gauntlet/worktrees/…``, where the `claude` CLI cannot
        write; this is the operator-chosen, journalled transaction that moves it
        to the writable root. Returns the old path, or ``None`` when the run is
        not a legacy one and this is an ordinary `same_tree` migration.

        **Why this is a release-and-recreate rather than a directory move.** The
        run's authoritative state — journal, manifest projection, transcripts —
        never lived in the tree (spike §4.4), and the branch ref is shared, so
        recreating at the new path from the branch reconstructs everything that
        matters. Moving the directory would additionally require ``git worktree
        repair`` to fix the admin entry's gitdir pointer, which is a second
        failure mode for no gain.

        **Why a dirty legacy tree is refused rather than snapshotted.**
        :func:`WT.release` would capture it into ``refs/gauntlet/recovery/…``
        and proceed, which satisfies R2 but converts the operator's visible
        work-in-progress into a recovery ref they have to know to look for. The
        clean-handoff invariant says the tree is normally clean, so a dirty one
        here means something is genuinely in flight and the operator should
        decide. Refusing costs them one command; the snapshot path costs them
        the discoverability of their own work.
        """
        entry = WT.legacy_observe(self.operator_root, man.branch, common_dir=common)
        if entry is None:
            return None
        excludes = self._run_tree_excludes(layout, man, run_dir)
        if entry.path.is_dir():
            work_root = entry.path  # the legacy RUN tree — a work root by construction
            try:
                dirt = gitops.status_porcelain(
                    work_root, exclude=excludes, untracked_all=True
                )
            except gitops.GitError:
                dirt = ""
            if dirt:
                listing = "\n  ".join(dirt.splitlines()[:8])
                raise MigrateWorktreeRefused(
                    f"refusing to relocate {man.slug!r}: its worktree at the "
                    f"pre-P7e location has uncommitted changes.\n"
                    f"  Tree inspected: {entry.path}\n  {listing}\n"
                    f"  Inspect it with: git -C {entry.path} status\n"
                    "Relocating recreates the tree at the new root from the "
                    "branch, so anything uncommitted there would survive only "
                    "as a recovery ref. Commit or discard it in that tree "
                    "first, then run `gauntlet migrate-worktree "
                    f"{man.slug}` again. Nothing has been moved or modified."
                )
        WT.release(
            self.operator_root, entry.path,
            slug=man.slug, run_id=man.run_id, excludes=excludes,
        )
        return entry.path

    def _migrate_locked(
        self, layout: "RunLayout", run_dir: Path, man: Manifest
    ) -> str:
        """:meth:`migrate_worktree`'s body, with the drive lock held."""
        slug, run_id, branch = man.slug, man.run_id, man.branch
        main_root = self._main_worktree_root()
        common = self._git_common_dir()
        relocated_from = self._relocate_legacy_worktree(layout, run_dir, man, common)
        try:
            wt = WT.ensure(
                self.operator_root, main_root,
                slug=slug, run_id=run_id, branch=branch,
            )
        except WT.WorktreeUnavailable as exc:
            # `WT.ensure` is NOT all-or-nothing: it verifies submodules (spike
            # §7) AFTER the tree has been created and locked, so a superproject
            # with uninitialized submodules raises having left a registered
            # worktree behind — and the resolver's rule 1 then answers
            # `dedicated`. Emitting the same_tree refusal there stated something
            # false about the run's mode (review F-004). So unwind whatever was
            # created, verify the unwind, and let the observed state write the
            # message.
            raise MigrateWorktreeRefused(
                self._unwound_refusal(
                    layout, run_dir, man,
                    f"could not create the run worktree for {slug!r}: {exc}\n"
                    f"  If git says the branch is already used by a worktree at "
                    f"{self.operator_root}, that is your OWN checkout — the "
                    "normal state for a same_tree run, and git's one-branch-"
                    "one-worktree rule (spike E2-A) is what refuses. This verb "
                    "will not check out or move a branch in your tree, so YOU "
                    f"choose: `git -C {self.operator_root} checkout "
                    f"{man.base_branch}` (or any branch that is not "
                    f"{branch!r}), then `gauntlet migrate-worktree {slug}` "
                    "again.",
                )
            ) from exc
        # Step 4 — write the export and prove the bookkeeping paths RESOLVE in
        # the new tree before anything is journalled. This is the step that can
        # still fail after a healthy `worktree add`: `StateDirNotContained` is
        # raised by the path builders when the run's state dir has no
        # counterpart under the work root, and finding that out AFTER appending
        # `WorktreeAdopted` would leave a run resolving `dedicated` into a tree
        # its own bookkeeping cannot be committed in.
        try:
            self._verify_export(wt, run_dir, layout, man)
        except BaseException as exc:
            # A KeyboardInterrupt/SystemExit keeps its own TYPE — laundering an
            # operator's Ctrl-C into "migration refused" would report a decision
            # the engine never made. The unwind runs either way, which is the
            # part that matters.
            if not isinstance(exc, Exception):
                self._discard_migrated_tree(wt.path, layout, man)
                raise
            raise MigrateWorktreeRefused(
                self._unwound_refusal(
                    layout, run_dir, man,
                    f"the run worktree for {slug!r} was created but its "
                    f"bookkeeping export could not be written or verified "
                    f"({exc}).",
                )
            ) from exc
        # Step 5. Recorded AFTER the tree exists — the mirror of rollback, which
        # records BEFORE it destroys; both orderings fail toward "the tree's
        # existence and the journal agree", which is what the resolver reads.
        #
        # Fail closed on an unrecorded adoption (review F-001). An earlier draft
        # kept the tree and warned, reasoning that rule 1 still answers
        # `dedicated` so the run is coherent. It is — until the tree is lost
        # together with its registration, at which point rule 2 is the ONLY
        # backstop and it is missing, and the run silently drops to driving the
        # operator's checkout. Migration is an explicit transaction whose entire
        # product is a durable mode change; if that cannot be recorded, the
        # honest outcome is that it did not happen. Checking the journal's
        # actual contents rather than `append_audit`'s ambiguous return is what
        # makes the unwind safe here: a partially-landed event reports as
        # recorded and is not unwound.
        if not self._record_worktree_adopted(
            run_dir, wt, slug=slug, run_id=run_id, migrated=True
        ):
            raise MigrateWorktreeRefused(
                self._unwound_refusal(
                    layout, run_dir, man,
                    f"the worktree for {slug!r} was created, but the adoption "
                    "could not be written to the journal — so nothing would "
                    "record that this run was migrated, and a later loss of the "
                    "tree would drop it back to driving your checkout without "
                    "saying so. Check the journal directory is writable, then "
                    "retry.",
                )
            )
        headline = (
            f"relocated {slug!r} from {relocated_from} to {wt.path}\n"
            "  That old location is under the git directory, where the `claude` "
            "CLI refuses to write (proposals/P7d-gate-blocker.md §2), so a "
            "builder or verifier step in it could fail silently.\n"
            if relocated_from is not None
            else f"migrated {slug!r} to a dedicated worktree at {wt.path}\n"
        )
        return (
            headline
            + f"  The run is unchanged: same branch ({branch}), same journal, "
            "same run dir in your checkout. Only the tree its agents edit "
            "moved.\n"
            f"  Your checkout was not touched. Inspect the run's tree with "
            f"`git -C {wt.path} status`; `gauntlet status {slug}` names it too.\n"
            f"  Undo with `gauntlet migrate-worktree {slug} --rollback`.\n"
            "  Note: a dedicated run commits the synced copy of "
            f"{layout.slug_dir.name}/prd.md on its own branch, so your local "
            "copy becomes an untracked duplicate. `gauntlet finish` sets that "
            "aside for you and lets the merge restore it as a tracked file when "
            "it is the same object (same bytes and file mode), naming what it "
            "replaced. If yours has DIVERGED it refuses instead and names both "
            "resolutions — that disagreement is yours to settle."
        )

    def _run_tree_excludes(
        self, layout: "RunLayout", man: Manifest, run_dir: Path
    ) -> list[str]:
        """What may legitimately sit uncommitted in a run's own worktree.

        The one derivation of that set, shared by every verb that inspects or
        tears down a run tree (`finish`, `migrate-worktree --rollback`). Two
        members, each earning its place differently:

        * the engine's **two-file export** — write-only with zero readers,
          regenerated on the next drive, and written by the engine itself, so
          counting it as a builder's work would block a teardown on the
          engine's own bookkeeping;
        * a **governed artifact PROVEN byte-identical** to the operator's
          authoritative copy. Proof, never category (review F-003): this list
          feeds ``WT.release``'s snapshot decision as well as the dirtiness
          refusal, so an artifact edited inside the run tree must fall out of it
          and be protected as the uncommitted work it is.

        Empty (and harmless) for a `same_tree` run or one with no tree: the
        callers' own guards cover those, and an empty exclusion list is the
        strict direction.
        """
        try:
            entry = WT.observe(
                self.operator_root, man.branch, main_root=self._main_worktree_root()
            )
        except gitops.GitError:
            return []
        if entry is None or not entry.path.is_dir():
            return []
        paths = RunPaths(
            repo_root=self.repo_root,
            work_root=entry.path,
            state_root=run_dir,
            artifact_root=layout.slug_dir,
        )
        return run_bookkeeping_excludes(
            entry.path, paths.bookkeeping_root, layout.slug_dir
        ) + self._verified_synced_artifacts(
            entry.path, paths.artifact_root_in_work, layout
        )

    def _verify_export(
        self, wt: "WT.RunWorktree", run_dir: Path, layout: "RunLayout",
        man: Manifest,
    ) -> None:
        """Write the §4.4 export and prove the §9.3 path builders answer.

        Two independent checks, because they fail for different reasons: the
        derived export dir must AGREE with the mirror `RunPaths` computes (a
        layout skew between :func:`WT.export_dir` and
        :attr:`RunPaths.bookkeeping_root` would put the committed bookkeeping
        somewhere no reader mirrors to), and the builders must then return a
        non-empty answer for files that actually exist on disk — the empty
        answer is the silent-degrade :class:`StateDirNotContained` exists to
        make loud.
        """
        paths = RunPaths(
            repo_root=self.repo_root,
            work_root=wt.path,
            state_root=run_dir,
            artifact_root=layout.slug_dir,
        )
        mirrored = paths.bookkeeping_root
        derived = WT.export_dir(
            wt.path, self.config.run_root, man.slug, man.run_id
        )
        if mirrored.resolve() != derived.resolve():
            raise _MigrationStepFailed(
                f"the export dir this engine derives ({derived}) is not where "
                f"the run's paths mirror to ({mirrored}). This is a layout "
                "skew, not an operator error — report it."
            )
        WT.write_bookkeeping_export(
            wt.path, run_dir, self.config.run_root, man.slug, man.run_id
        )
        # Existence-independent allowlist first (it raises on an uncontained
        # state dir), then the existence-filtered set the checkpoint commit
        # actually stages.
        engine_bookkeeping_candidates(wt.path, mirrored)
        staged = run_bookkeeping_paths(wt.path, mirrored)
        if not staged:
            raise _MigrationStepFailed(
                f"no bookkeeping file resolved inside the new run worktree at "
                f"{wt.path} (expected the export at {mirrored}). Without it the "
                "FR-2.2 checkpoint commit would have nothing in-tree to stage."
            )

    def _verified_synced_artifacts(
        self, work_root: Path, artifact_root_in_work: Path, layout: "RunLayout"
    ) -> list[str]:
        """Governed artifacts in the run tree PROVEN identical to the authority.

        Review F-003. The rollback excludes governed artifacts from both the
        dirtiness refusal and :func:`WT.release`'s snapshot decision, on the
        grounds that they are redundant copies of the operator's authoritative
        file. That is true right after a sync and false the moment anything
        edits the tree copy — and "the playbook says not to edit it there" is
        not proof that nobody did. An unproven exclusion here does not merely
        skip a warning: it deletes the bytes.

        So each artifact earns its exclusion by comparison. Both sides must be
        regular files (a symlink on either side is a different object, not a
        copy, and is never excluded) with identical bytes. Anything else —
        divergent, missing on either side, unreadable — is treated as
        uncommitted work, which makes the rollback refuse and name it.

        **The worktree plane is not the only plane** (review F-001). Byte
        equality is a claim about the FILE; excluding the path drops it from the
        dirtiness check *and* from :func:`WT.release`'s snapshot decision, and a
        linked worktree has its own private index that dies with it. A path
        staged at version B while the working tree holds an
        authority-identical C would be judged redundant, skip the snapshot, and
        take B to the grave — the staged/unstaged-differ row the plan's §7
        matrix names explicitly, and the one state a working-tree comparison
        structurally cannot see.

        So the index plane must be proven empty too. The accepted shapes are
        exactly: no status entry at all (tracked and clean), ``??``
        (untracked — no index state exists to lose), and ``' M'`` (index equals
        HEAD, worktree modified — both versions survive, one in the branch and
        one as the authority). Everything else — anything staged, unmerged, a
        type change, a rename — keeps its protection.
        """
        verified: list[str] = []
        for rel in governed_artifact_paths(work_root, artifact_root_in_work):
            in_tree = work_root / rel
            authority = layout.slug_dir / Path(rel).name
            try:
                if not (
                    in_tree.is_file()
                    and not in_tree.is_symlink()
                    and authority.is_file()
                    and not authority.is_symlink()
                    and in_tree.read_bytes() == authority.read_bytes()
                    # Mode is its own plane (review F-002, plan §7): a copy that
                    # differs only in the executable bit is a different object,
                    # and dropping its protection would silently lose that bit.
                    and (in_tree.stat().st_mode & 0o111)
                    == (authority.stat().st_mode & 0o111)
                ):
                    continue
            except OSError:
                continue  # cannot prove identical → do not exclude
            if self._path_has_index_state(work_root, rel):
                continue  # F-001: staged/unmerged state the file cannot show
            verified.append(rel)
        return verified

    # Porcelain XY codes for a path carrying NOTHING unique in the index: no
    # entry at all, untracked, or index-equals-HEAD with a modified worktree.
    _NO_INDEX_STATE_CODES = ("??", " M")

    @staticmethod
    def _path_has_index_state(work_root: Path, rel: str) -> bool:
        """True when ``rel`` holds index-plane state a teardown would destroy.

        Fail-closed in both error directions: an unreadable status is treated as
        "there IS state", because the caller uses this to decide whether it may
        stop protecting the path, and "I could not look" must never read as
        "there is nothing there".
        """
        try:
            out = gitops.status_porcelain(work_root, paths=[rel], untracked_all=True)
        except gitops.GitError:
            return True
        for line in out.splitlines():
            if not line.strip():
                continue
            if line[:2] not in RunManager._NO_INDEX_STATE_CODES:
                return True
        return False

    def _rollback_refusal(self, blocker: str, man: Manifest) -> str:
        """A rollback refusal, closing with the mode it actually leaves (F-007).

        Every path here has already been through the resolver, so the mode is
        observed rather than assumed: `dedicated` for the refusals reached after
        eligibility resolved that way (live driver, terminal, born-dedicated),
        and `same_tree` for the one that fires precisely BECAUSE the run is not
        dedicated — where the old fixed text was right by accident.
        """
        try:
            mode = self._effective_worktree_mode(man)
        except Exception:
            mode = None
        return self._still_resumable(blocker, man, mode=mode)

    def _discard_migrated_tree(
        self, path: Path, layout: "RunLayout", man: Manifest
    ) -> None:
        """Remove a worktree whose migration failed after it was created (§10.4).

        Best-effort: the caller is already raising the real cause, and a cleanup
        failure must not replace it (the same lesson as P7c-1.1's
        `merge --abort` fix). Whether it SUCCEEDED is not assumed — the caller
        re-observes and lets the observed state write the message (F-004).

        No ``WorktreeReleased`` is appended because no ``WorktreeAdopted`` was:
        the journal records transitions that happened, and a migration that
        never completed is not one.

        The export the failed step may have written is excluded from the
        dirtiness check, so this removal does not stop to snapshot the engine's
        own bookkeeping; genuinely uncommitted work in the tree still is
        (R2 — :func:`WT.release` snapshots before any ``--force``).
        """
        try:
            excludes = run_bookkeeping_excludes(
                path,
                WT.export_dir(path, self.config.run_root, man.slug, man.run_id),
                layout.slug_dir,
            )
        except Exception:
            excludes = None
        try:
            WT.release(
                self.operator_root, path,
                slug=man.slug, run_id=man.run_id, excludes=excludes,
            )
        except Exception:
            pass  # the caller's raise carries the real cause

    def _unwound_refusal(
        self, layout: "RunLayout", run_dir: Path, man: Manifest, blocker: str
    ) -> str:
        """Unwind a partial migration, VERIFY it, and say what is actually true.

        Review F-004. Every pre-adoption failure used to emit the fixed
        "nothing was moved … remains fully drivable in `same_tree`" clause, but
        two paths reach it with a registered worktree still on disk:
        :func:`WT.ensure` verifies submodules (spike §7) only AFTER creating and
        locking the tree, and the export unwind swallowed every
        :func:`WT.release` failure. In both, the resolver's rule 1 then answers
        `dedicated` — so the refusal was making a false statement about which
        tree the run's agents would edit next, at the moment the operator was
        trying to establish exactly that.

        So: discard whatever exists at this run's derived path, re-observe, and
        branch on the observation. Restored → the same_tree clause, now earned.
        Not restored → name the surviving tree, the mode it actually leaves, and
        the executable action that finishes the job. Never a claim we did not
        verify, in either direction.
        """
        try:
            entry = WT.observe(
                self.operator_root, man.branch, main_root=self._main_worktree_root()
            )
        except gitops.GitError:
            entry = None
        if entry is not None:
            self._discard_migrated_tree(entry.path, layout, man)
        try:
            survivor = WT.observe(
                self.operator_root, man.branch, main_root=self._main_worktree_root()
            )
        except gitops.GitError as exc:
            return (
                f"{blocker}\n"
                f"The migration was unwound, but git's worktree list could not "
                f"be read afterwards ({exc}), so this cannot prove which tree "
                f"the run now drives. Check `gauntlet status {man.slug}` before "
                "resuming."
            )
        if survivor is None:
            return self._still_resumable(blocker, man, mode=WT.MODE_SAME_TREE)
        return (
            f"{blocker}\n"
            f"The migration was unwound, but the run worktree at "
            f"{survivor.path} could NOT be removed, so this run is now in "
            "`dedicated` mode — not `same_tree`. It is not wedged and no state "
            "was lost: the branch, the journal and the run dir are untouched, "
            "and the run drives that tree.\n"
            f"  To finish returning it: `gauntlet migrate-worktree {man.slug} "
            "--rollback`.\n"
            f"  To drive this run in your own checkout right now: `gauntlet "
            f"resume {man.slug} --same-tree`.\n"
            f"  To inspect the tree: `git -C {survivor.path} status`."
        )

    def rollback_worktree_migration(self, slug: str) -> str:
        """Return a migrated run to `same_tree`, journal intact (spike §10).

        ``worktree unlock`` + ``worktree remove`` + ``WorktreeReleased``, which
        is all it takes because §4.4 never moved the journal: the authoritative
        state, the manifest projection, the transcripts, the heartbeat and the
        recovery intent were in the operator's checkout the whole time and are
        untouched here. What is destroyed is the tree — so a tree with
        uncommitted work REFUSES rather than sweeping it into a recovery ref
        the operator did not ask for and would not know to look for (F-011).

        After it returns, the resolver answers `same_tree` again by its own
        rules: no registered worktree (rule 1 fails), a `WorktreeReleased` after
        the `WorktreeAdopted` (rule 2 fails), and no recorded birth mode (rule 3
        is why a run BORN dedicated is refused above, not rolled back).
        """
        safe_run_segment(slug, kind="slug")
        self._refuse_migration_in_pipeline_context(
            "gauntlet migrate-worktree --rollback"
        )
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        self._reconcile_projection(run_dir, slug)
        man = Manifest.load(run_dir / "manifest.json")
        liveness = self._migration_liveness(slug, run_dir)
        blocker = self._migration_rollback_blocker(man, liveness=liveness)
        if blocker is not None:
            raise MigrateWorktreeRefused(self._rollback_refusal(blocker, man))
        handle = self._acquire_worktree_lock(slug, man.run_id, run_dir=run_dir)
        try:
            man = Manifest.load(run_dir / "manifest.json")
            blocker = self._migration_rollback_blocker(man, liveness=liveness)
            if blocker is not None:
                raise MigrateWorktreeRefused(self._rollback_refusal(blocker, man))
            return self._rollback_migration_locked(layout, run_dir, man)
        finally:
            self._release_worktree_lock(handle)

    def _rollback_migration_locked(
        self, layout: "RunLayout", run_dir: Path, man: Manifest
    ) -> str:
        """:meth:`rollback_worktree_migration`'s body, with the drive lock held."""
        try:
            entry = WT.observe(
                self.operator_root, man.branch, main_root=self._main_worktree_root()
            )
        except gitops.GitError as exc:
            raise MigrateWorktreeRefused(
                self._rollback_refusal(
                    f"could not read git's worktree list ({exc}), so this "
                    "cannot prove which tree it would remove.",
                    man,
                )
            ) from exc
        if entry is None:
            # The §11-row-2 shape reached through rollback: the tree is gone
            # but the journal still records the adoption, so the resolver
            # answers `dedicated` and the next resume would REBUILD it. Closing
            # the adoption is the whole of the rollback here — and it is why
            # this branch exists rather than reporting "nothing to do".
            if not self._record_worktree_released(
                run_dir, WT.run_worktree_path(
                    self._main_worktree_root(), man.slug, man.run_id
                ),
                slug=man.slug, run_id=man.run_id,
            ):
                raise MigrateWorktreeRefused(
                    self._rollback_refusal(
                        f"the release of {man.slug!r} could not be written to "
                        "the journal, and closing the open adoption is the ONLY "
                        "thing this rollback does when the tree is already gone "
                        "— so reporting success would be reporting nothing. "
                        "Check the journal directory is writable, then retry.",
                        man,
                    )
                )
            return (
                f"rolled back {man.slug!r} to `same_tree`: its worktree was "
                "already gone, and the journal's open adoption is now closed "
                "so the next resume drives your checkout instead of rebuilding "
                "a tree.\n"
                f"  Branch {man.branch} and every commit on it are untouched."
            )
        # What may sit legitimately uncommitted in a run worktree — the engine's
        # write-only export, and a governed artifact PROVEN byte-identical to
        # the operator's authoritative copy (review F-003). Derived by
        # `_run_tree_excludes`, which `finish` now shares (P7d): two callers
        # asking the same question of the same tree must not answer it twice.
        excludes = self._run_tree_excludes(layout, man, run_dir)
        self._refuse_if_run_worktree_dirty(
            man, verb="migrate-worktree --rollback",
            exc_type=MigrateWorktreeRefused, excludes=excludes,
        )
        if not self._record_worktree_released(
            run_dir, entry.path, slug=man.slug, run_id=man.run_id,
        ):
            # Recorded BEFORE the tree is removed, and fail closed if it did not
            # land (review F-001). The other ordering loses either way: a
            # removal followed by a failed append leaves no tree and an OPEN
            # adoption, so the resolver answers `dedicated`, the next resume
            # rebuilds the tree the operator just removed, and the verb has
            # already said it succeeded. This way the failure leaves the tree
            # intact and the run coherent, and a retry is safe.
            raise MigrateWorktreeRefused(
                self._rollback_refusal(
                    f"the release of {man.slug!r} could not be written to the "
                    "journal, so the rollback was not performed — the worktree "
                    f"at {entry.path} is untouched. Removing it while the "
                    "journal still recorded an open adoption would make the "
                    "next resume rebuild it. Check the journal directory is "
                    "writable, then retry.",
                    man,
                )
            )
        ref = WT.release(
            self.operator_root, entry.path,
            slug=man.slug, run_id=man.run_id, excludes=excludes,
        )
        if ref:
            # Not expected — the refusal above covers real dirt — but a
            # snapshot the operator does not know about is exactly the thing
            # F-011 objected to, so it is never silent.
            self._warn(
                run_dir,
                f"rollback snapshotted uncommitted work from the run worktree "
                f"before removing it; recover it from {ref}",
            )
        return (
            f"rolled back {man.slug!r} to `same_tree`: removed the run "
            f"worktree at {entry.path}.\n"
            f"  Branch {man.branch}, its commits, the journal and the run dir "
            "are all untouched — §4.4 never moved them. The next "
            f"`gauntlet resume {man.slug}` drives your own checkout again.\n"
            f"  Re-migrate at any time with `gauntlet migrate-worktree "
            f"{man.slug}`."
        )

    # ---- status -------------------------------------------------------------
    def status(self, slug: str) -> Manifest:
        layout = self.layout(slug)
        return Manifest.load(layout.active_run_dir() / "manifest.json")

    # ---- usage-ledger backfill (harness-efficiency FR-10.1) -----------------
    def _iter_run_manifests(self) -> "list[Manifest]":
        """Every parseable run manifest under the run root (``run_root/*/*/``).

        Scans the on-disk layout the orchestrator writes — one manifest per run
        instance. A malformed/torn manifest is skipped (fail-safe: backfill is a
        best-effort reconstruction, never a run-halting parse). Deduplicated by
        run_id so a slug's `active-run.txt` pointer plus its run dir don't yield
        the same manifest twice.
        """
        run_root = self.repo_root / self.config.run_root
        manifests: list[Manifest] = []
        seen: set[str] = set()
        for manifest_path in sorted(run_root.glob("*/*/manifest.json")):
            try:
                man = Manifest.load(manifest_path)
            except (OSError, ValueError):
                continue
            if man.run_id in seen:
                continue
            seen.add(man.run_id)
            manifests.append(man)
        return manifests

    def backfill_ledger(self, *, ledger_path: Path | None = None):
        """Reconstruct the machine-global usage ledger from existing manifests.

        A one-shot, idempotent operator command (FR-10.1): so the median estimator
        has history from the first enforced run instead of a cold start. Re-running
        it appends nothing (de-dup by ``run_id::step_id``). Returns a
        ``ledger.BackfillResult`` (manifests scanned, rows added vs skipped).
        """
        from gauntlet.engine.ledger import backfill_from_manifests

        return backfill_from_manifests(
            self._iter_run_manifests(),
            repo_root=self.repo_root,
            config=self.config,
            path=ledger_path,
        )

    # ---- feedback (FR-6.1) --------------------------------------------------
    def save_feedback(self, slug: str, data, *, run_dir: Path | None = None) -> Path:
        """Capture human feedback into the run's ``retro/feedback.md`` (+ json)."""
        from gauntlet.engine.feedback import write_feedback

        layout = self.layout(slug)
        run_dir = run_dir or layout.active_run_dir()
        if not data.run_id and (run_dir / "manifest.json").exists():
            data.run_id = Manifest.load(run_dir / "manifest.json").run_id
        return write_feedback(run_dir, data, self.writer)

    def regenerate_proposals(
        self, slug: str, *, run_dir: Path | None = None, adapter_factory=None
    ) -> list:
        """Re-run proposal synthesis for a run, picking up feedback (FR-6.1→6.3).

        FR-6.1 requires feedback captured "at run end or later" to be able to
        drive proposal generation. The retrospective step reads feedback once
        during the run, so feedback entered afterwards (via ``gauntlet
        feedback``) would otherwise never reach synthesis (review F-001). This
        re-synthesises from the run's saved self-critiques + the now-present
        feedback, APPENDING any new pending proposals under ``retro/proposals/``
        (prior proposals are never clobbered — data over inference).

        Returns the proposals generated this pass (possibly empty). Returns
        ``[]`` when the run's pipeline has no retrospective step or no proposer.
        """
        from gauntlet.engine import retro
        from gauntlet.engine.execution import StepContext
        from gauntlet.engine.feedback import read_feedback
        from gauntlet.engine.manifest import StepRecord
        from gauntlet.engine.steptypes import _UsageAccumulator

        layout = self.layout(slug)
        run_dir = run_dir or layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")
        pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")

        step = next(
            (s for s in pipeline.all_steps() if s.type == "retrospective"), None
        )
        if step is None or not step.get("proposer"):
            return []
        proposer = step.get("proposer")

        critiques: dict[str, str] = {}
        for agent in step.get("agents") or []:
            crit = run_dir / "retro" / f"retro-{agent}.md"
            if crit.exists():
                critiques[agent] = crit.read_text()
        feedback = read_feedback(run_dir)

        rec = man.record("retrospective") or StepRecord(
            id="retrospective", type="retrospective"
        )
        ctx = StepContext(
            repo_root=self.repo_root,
            run_dir=run_dir,
            artifact_root=layout.slug_dir,
            config=self.config,
            pipeline=pipeline,
            manifest=man,
            record=rec,
            writer=self.writer,
            excludes=run_bookkeeping_excludes(self.repo_root, run_dir, layout.slug_dir),
            adapter_factory=adapter_factory,
        )
        usage = _UsageAccumulator()
        summary = retro.build_run_summary(ctx)
        return retro._generate_proposals(
            ctx, step, summary, critiques, feedback, proposer, usage
        )

    # ---- proposals (FR-6.3/6.4) ---------------------------------------------
    def _all_slugs(self) -> list[str]:
        root = self.repo_root / self.config.run_root
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    def _iter_run_dirs(self, slug: str | None = None):
        slugs = [slug] if slug else self._all_slugs()
        for s in slugs:
            sdir = self.layout(s).slug_dir
            if not sdir.exists():
                continue
            for run_dir in sorted(sdir.glob("run-*")):
                if (run_dir / "manifest.json").exists():
                    yield run_dir

    def list_proposals(self, slug: str | None = None) -> list[tuple[Path, object]]:
        """Every proposal across runs (optionally one slug), as (run_dir, Proposal)."""
        from gauntlet.engine.proposals import list_proposals

        out: list[tuple[Path, object]] = []
        for run_dir in self._iter_run_dirs(slug):
            for p in list_proposals(run_dir / "retro" / "proposals"):
                out.append((run_dir, p))
        return out

    def review_proposals(self, slug: str | None = None, *, decide, timestamp=None) -> list[dict]:
        """Present pending proposals to ``decide`` and apply/reject each (FR-6.4).

        ``decide(proposal) -> (action, notes)`` where action is ``approve`` or
        ``reject``; the CLI wires it to interactive prompts, tests pass a
        callback. Approved diffs are applied on a clean tree and committed — no
        proposal self-applies (this is an engine action gated on human approval).
        Per-proposal failures are recorded, never aborting the whole review.
        """
        from gauntlet.engine import proposals as P
        from gauntlet.engine.execution import run_bookkeeping_excludes

        timestamp = timestamp or _utc_stamp()
        changelog = self.repo_root / self.config.asset_root / "prompts" / "CHANGELOG.md"
        identity = self.config.identity("retro")
        results: list[dict] = []
        for run_dir, proposal in self.list_proposals(slug):
            if proposal.status != P.PENDING or not proposal.valid:
                continue
            action, notes = decide(proposal)
            if action != "approve":
                P.reject_proposal(proposal, notes or "")
                results.append({"proposal": proposal.name, "action": "rejected"})
                continue
            excludes = run_bookkeeping_excludes(self.repo_root, run_dir, run_dir.parent)
            if not gitops.is_clean(self.operator_root, exclude=excludes):
                raise P.ProposalError(
                    "refusing to apply a proposal: worktree is dirty; commit or "
                    "discard changes first (governed apply needs a clean tree)"
                )
            try:
                sha = P.apply_proposal(
                    self.repo_root, proposal, identity=identity,
                    changelog_path=changelog, timestamp=timestamp,
                    asset_root=self.config.asset_root,
                )
                results.append({"proposal": proposal.name, "action": "applied", "sha": sha})
            except P.ProposalError as exc:
                results.append({"proposal": proposal.name, "action": "error", "reason": str(exc)})
        return results

    # ---- trend metrics (FR-6.6) ---------------------------------------------
    def trend(self, slug: str | None = None) -> list:
        from gauntlet.engine.trend import build_run_trend

        rows = []
        for run_dir in self._iter_run_dirs(slug):
            man = Manifest.load(run_dir / "manifest.json")
            rows.append(build_run_trend(man, judge_audit_path=run_dir / "judge-audit.jsonl"))
        rows.sort(key=lambda r: r.run_id)
        return rows

    # ---- rollback (FR-9.9 / review F-010) -----------------------------------
    def rollback(self, slug: str, phase: int) -> str:
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        # P6: reconcile the projection with the authoritative journal before
        # anything reads it (plan §4.6/§5.5/R8).
        self._reconcile_projection(run_dir, slug)
        man = Manifest.load(run_dir / "manifest.json")
        before_fp = self._capture_progress(slug)  # R5 guard input
        # Rollback is a worktree-mutating verb: take the drive lock so a live
        # driver can never race the rewind (PR #77 review), exactly like
        # resume/abort.
        handle = self._acquire_worktree_lock(slug, man.run_id, run_dir=run_dir)
        try:
            # Converge any surviving recovery intent BEFORE the guards run
            # (post-P3 review F-002): a rollback killed between its Git apply
            # and its manifest persist leaves the branch reset but the
            # manifest un-rewound — the tier-2 agreement guard would then
            # refuse ("behind") forever, so a retried rollback could never
            # reach the executor's own survivor replay. Reload the manifest
            # after a replay: the site finisher rewrote it.
            # Rollback rewinds a WORKING tree (checkout/reset/clean), so it
            # must run in the run's own tree — spike E8-B measured the
            # isolation this buys: a hard reset there leaves the operator's
            # branch, HEAD, index and reflog untouched.
            with self._worktree_paths_or_park(
                layout, run_dir, man, mode=self._effective_worktree_mode(man)
            ):
                if RX.replay_pending_intent(self.work_root, run_dir) is not None:
                    man = Manifest.load(run_dir / "manifest.json")
                target = self._rollback_locked(layout, run_dir, man, phase)
        finally:
            self._release_worktree_lock(handle)
        # R5: a repeated rollback to the same boundary that changes nothing —
        # branch, manifest, index, and worktree all identical — is a no-op
        # loop, not a success (plan §4.5). Rollback is never a human wait.
        self._require_progress_after(
            slug, before_fp, verb="rollback", exempt_human_waits=False
        )
        return target

    def _rollback_locked(self, layout, run_dir, man: Manifest, phase: int) -> str:
        # Rollback rewinds the RUN BRANCH (FR-9.9) — never whatever branch
        # happens to be checked out (PR #77 review, blocking finding). P3
        # moves EVERY guard and resolution BEFORE the checkout (plan §6 P3 /
        # post-177d721 F-004): the guards read the run-branch REF explicitly,
        # so a refused rollback leaves the operator's checkout, index, and
        # worktree untouched — prevalidation is observational.
        # WORK tree: rollback rewinds the RUN's branch and tree; it must never
        # reach into the operator's checkout (P7 acceptance A1).
        repo = self.work_root
        if not gitops.branch_exists(repo, man.branch):
            raise RollbackGuardError(
                f"refusing rollback: run branch {man.branch!r} is missing; "
                "restore it (e.g. from refs/gauntlet/backup/ or "
                "refs/gauntlet/recovery/) first"
            )
        # Guard 1: clean work tree — checked BEFORE any checkout (switching
        # branches over uncommitted work could carry or clobber it). Only the
        # engine's own bookkeeping is excluded (review F-001), so an
        # uncommitted real artifact still blocks.
        excludes = run_bookkeeping_excludes(
            self.work_root, self._bookkeeping_root(run_dir),
            self._artifact_root_in_work(layout),
        )
        if not gitops.is_clean(self.work_root, exclude=excludes):
            raise RollbackGuardError(
                "refusing rollback: worktree is dirty; commit or discard "
                f"first.\n  Tree inspected: {self.work_root}\n"
                f"  Inspect it with: git -C {self.work_root} status"
            )
        # Guard 2: branch tip must AGREE with the manifest's last recorded
        # commit before a rewind (FR-9.9). Three tiers (#62/#72), all read
        # from the branch REF — no checkout needed:
        # - tip == last recorded, or ahead by ONLY engine bookkeeping commits
        #   (response checkpoints — never appended to man.commits): proceed.
        # - tip a strict DESCENDANT of the last recorded commit (unmanifested
        #   real commits — e.g. a builder killed after committing wip but
        #   before a manifest flush, then `recover`ed): absorb them. The
        #   recovery snapshot + manifest snapshot capture the tip first, so
        #   the reset is reversible, and the absorption is recorded as a
        #   manifest warning. Refusing this case gave the exact recovery verb
        #   meant for a killed phase no path forward except forbidden git
        #   surgery (#72).
        # - anything else (fork, or tip BEHIND — recorded commits missing):
        #   refuse, exactly as before. That is the p3-F-003 protection: a
        #   rewind must never silently discard a state the snapshot cannot
        #   represent as a linear ancestor range.
        if not man.commits:
            raise RollbackGuardError("no recorded commits to roll back to")
        last_recorded = man.commits[-1].sha
        tip = gitops.rev_parse(repo, f"refs/heads/{man.branch}")
        absorbed: str | None = None
        if tip != last_recorded and not gitops.advance_is_engine_bookkeeping(
            self.repo_root, last_recorded,
            bookkeeping=engine_bookkeeping_candidates(self.repo_root, run_dir),
            tip=tip,
        ):
            if not gitops.is_ancestor(self.repo_root, last_recorded, tip):
                raise RollbackGuardError(
                    "refusing rollback: branch has diverged from the manifest "
                    f"(tip {tip[:10]} is not a descendant of the last "
                    f"recorded {last_recorded[:10]}); the branch and manifest "
                    "must agree before a rewind (FR-9.9)"
                )
            absorbed = gitops.log_range(self.repo_root, last_recorded, tip)
        # Resolve the target: the last commit whose phase prefix is P<phase> —
        # BEFORE any checkout, so an unknown phase refuses observationally.
        target = self._phase_boundary_sha(man, phase)
        if target is None:
            raise RollbackGuardError(
                f"no recorded phase-{phase} commit boundary to roll back to"
            )
        current_branch = gitops.current_branch(repo)
        human_patterns = human_owned_excludes(excludes)
        if current_branch != man.branch and gitops.dirty_paths_matching(
            repo, human_patterns
        ):
            # PR #77 confirm review: PR.md is intentionally excluded from the
            # generic dirty guard, but a checkout can still refuse when those
            # edits conflict with the run branch. Fail before switching
            # branches with a precise, sanctioned resolution instead of
            # leaking a raw git error or risking a carry-over onto the wrong
            # branch.
            raise RollbackGuardError(
                "refusing rollback: the current branch has uncommitted "
                "human-owned PR.md state; commit or move those edits before "
                f"switching to run branch {man.branch!r}"
            )

        # --- read-only assessment (plan §4.2) --------------------------------
        git_obs = RX.observe_git(
            repo,
            run_branch=man.branch,
            recorded_sha=last_recorded,
            excludes=excludes,
            # F-002: carrier-derived, like resume. `repo` here is the WORK
            # tree, so handing it the operator-checkout run dir / slug dir
            # makes `_tree_rel` raise StateDirNotContained under `dedicated`
            # and rollback dies before it reaches the executor.
            bookkeeping_candidates=engine_bookkeeping_candidates(
                repo, self._bookkeeping_root(run_dir)
            ),
            approved_artifacts=governed_artifact_paths(
                repo, self._artifact_root_in_work(layout)
            ),
        )
        state_obs = RX.observe_state(man, None, liveness=RX.DriverLiveness.NONE)

        def fingerprint() -> "RX.ProgressFingerprint":
            return RX.build_progress_fingerprint(
                repo, manifest=man, excludes=excludes
            )

        reason = f"rollback {man.slug} to phase P{phase}"
        action = RX.SnapshotAndRestartAction(
            description=f"snapshot every plane, then reset {man.branch} to "
            f"the P{phase} boundary {target[:10]}",
            target_ref=f"refs/heads/{man.branch}",
            target_sha=target,
            reason=reason,
        )
        assessment = RX.RecoveryPlanner(repo).assess_rewind(
            git_obs=git_obs,
            state_obs=state_obs,
            fingerprint=fingerprint(),
            action=action,
            cause=(
                RX.RecoveryCause.BRANCH_AHEAD if absorbed is not None
                else RX.RecoveryCause.NONE
            ),
            evidence=(f"operator-requested rollback to P{phase} (FR-9.9)",),
        )
        # Governed-artifact discards are audited loudly (R9/FR-10.4): the
        # planner records each as evidence, and the persist step below turns
        # them into manifest warnings. Never a refusal — hand-editing and
        # committing the PRD/plan is a sanctioned operator workflow.
        governed_notes = [
            e for e in assessment.evidence
            if e.startswith(RX.GOVERNED_DISCARD_EVIDENCE_PREFIX)
        ]
        spec = RX.RewindSpec(
            site="run.rollback",
            checkout_branch=man.branch,
            target_sha=target,
            reset_mode=RX.RESET_PLAIN,
            clean=False,  # rollback never cleans untracked files (unchanged)
        )

        # Manifest snapshot before any rewind (F-010); lives in the ignored
        # run-instance dir, so it does not perturb the assessed fingerprint.
        ts = _utc_stamp()
        shutil.copy2(run_dir / "manifest.json", run_dir / f"manifest.snapshot-{ts}.json")

        def persist(result: "RX.RecoveryResult") -> None:
            # Step 7 of the transaction: the manifest-side state transition.
            # Also re-runnable by the registered replay finisher when a crash
            # lands between apply and this persist.
            if absorbed is not None:
                # Loud absorption (#72): the discarded-but-preserved range is
                # part of the audit trail, never a silent reset side effect.
                n = len(absorbed.splitlines())
                man.warnings.append(
                    f"rollback absorbed {n} unmanifested commit(s) above the "
                    f"last recorded commit {last_recorded[:10]} (branch was "
                    "ahead of the manifest — e.g. a builder killed before a "
                    "manifest flush); preserved in recovery snapshot "
                    f"{result.snapshot.ref}:\n{absorbed}"
                )
            for note in governed_notes:
                # Loud governance audit (R9/FR-10.4, post-P3 review F-004):
                # this rollback discarded an operator commit that modified a
                # governed artifact; the state is preserved in the snapshot
                # and the discard is part of the audit trail.
                man.warnings.append(f"rollback: {note}")
            _apply_rollback_manifest_transition(
                man, run_dir, target=target, phase=phase, at=_utc_stamp()
            )

        executor = RX.RecoveryExecutor(
            # repo_root is the OPERATOR's checkout — it resolves the run-instance
            # dir, the drive lock and the projection, all of which stay there by
            # design (spike §4.4). work_root is the tree the rewind mutates.
            self.operator_root,
            run_dir,
            run_id=man.run_id,
            run_root=self.config.run_root,
            excludes=excludes,
            work_root=repo,
        )
        executor.apply(
            assessment,
            action,
            spec=spec,
            snapshot_request=RX.SnapshotRequest(
                snapshot_id=f"rollback-P{phase}-{ts.replace(':', '-').replace('+', '-')}",
                reason=reason,
                run_branch=man.branch,
                exclude=excludes,
                protected=human_patterns,
            ),
            fingerprint=fingerprint,
            persist=persist,
            payload={
                "phase": phase,
                "absorbed": absorbed,
                "last_recorded": last_recorded,
            },
        )
        return target

    def _rewind_manifest(self, man: Manifest, run_dir: Path, target: str) -> None:
        """Rewind the manifest to match the reset branch (review F-002)."""
        _rewind_manifest_state(man, run_dir, target)

    # ---- internals ----------------------------------------------------------
    def _phase_boundary_sha(self, man: Manifest, phase: int) -> str | None:
        prefix = f"P{phase}"
        match = None
        for commit in man.commits:
            head = commit.phase.split(".")[0]  # P3.1 -> P3
            if head == prefix:
                match = commit.sha
        return match

    def _drive(self, layout, run_dir, pipeline, man, *, use_judge, adapter_factory,
               extra_context, clock, response_action=None,
               interrupted_override=None) -> str:
        # Suspend/sleep resilience (FR-5): a heartbeat writer runs for the life of
        # the drive so a host sleep is detectable + creditable (via the process-
        # global registry adapters/process.py polls), and — opt-in — `caffeinate`
        # keeps the host awake. Both are no-ops off their preconditions, so a run
        # with the defaults behaves exactly as before.
        from gauntlet.engine.heartbeat import HeartbeatWriter, KeepAwake

        if self.config.keep_awake and sys.platform != "darwin":
            warnings.warn(
                "keep_awake: true is ignored on this non-darwin platform "
                f"({sys.platform}); `caffeinate` is a macOS tool (FR-5.4).",
                stacklevel=2,
            )
        writer = HeartbeatWriter(
            run_dir,
            interval_s=self.config.heartbeat_interval_s,
            credit_cap_s=self.config.suspend_credit_cap_s,
        )
        with KeepAwake(enabled=self.config.keep_awake), writer:
            try:
                if not use_judge:
                    orch = self._orchestrator(
                        layout, run_dir, pipeline, man, judge_env={},
                        adapter_factory=adapter_factory,
                        extra_context=extra_context, clock=clock,
                        response_action=response_action,
                        interrupted_override=interrupted_override)
                    status = orch.drive()
                else:
                    status = self._with_judge(man, run_dir, lambda env: self._orchestrator(
                        layout, run_dir, pipeline, man, judge_env=env,
                        adapter_factory=adapter_factory, extra_context=extra_context,
                        clock=clock, response_action=response_action,
                        interrupted_override=interrupted_override).drive())
            finally:
                # Fold any detected suspension intervals into the manifest (the
                # orchestrator is the sole in-drive manifest writer, so this drains
                # only after driving stops — no concurrent write). Best-effort.
                self._drain_suspensions(writer, man, run_dir)
        self._maybe_draft_pr(layout, run_dir, man, status)
        return status

    def _drain_suspensions(self, writer, man: Manifest, run_dir: Path) -> None:
        """Append heartbeat-detected suspension intervals to the manifest (FR-5.1)."""
        intervals = writer.drain_suspensions()
        if not intervals:
            return
        man.suspensions.extend(
            M.Suspension(start=s.start, end=s.end, gap_s=s.gap_s) for s in intervals
        )
        man.write_atomic(run_dir / "manifest.json")

    def _maybe_draft_pr(self, layout, run_dir, man, status: str) -> None:
        """Draft runs/<slug>/PR.md at final-gate pass (FR-9.8); never opens it.

        Owned by the RunManager (not the orchestrator) because PR.md is a
        slug-dir deliverable a human edits and commits — opening and pushing
        stay human actions (PRD §2.2).

        PR.md is a REQUIRED final-gate artifact (FR-9.8), so a failure to render
        it is not swallowed (review F-005): the error is recorded as a manifest
        warning, persisted, and re-raised. Fail closed and data over inference —
        a completed run never silently returns RUN_DONE with the deliverable
        missing and no trace of why.
        """
        if status != M.RUN_DONE:
            return
        from gauntlet.engine.pr import write_pr_draft

        try:
            write_pr_draft(layout.slug_dir, run_dir, man, self.writer)
        except Exception as exc:
            man.warnings.append(
                f"FR-9.8 PR.md draft failed at final-gate pass: {exc!r}"
            )
            man.write_atomic(run_dir / "manifest.json")
            raise

    def _with_judge(self, man, run_dir, fn):
        judge_model = None
        if "judge_llm" in self.config.agents:
            judge_model = self.config.agents["judge_llm"].model
        judge = ManagedJudge(
            policy_path=self.repo_root / self.config.asset_root / "policy.yaml",
            audit_path=run_dir / "judge-audit.jsonl",
            run_id=man.run_id,
            judge_model=judge_model,
            # The fixed path boundary the agent's hooks check writes against
            # (notes #29). It must be the tree the agent actually edits (P7c,
            # spike §9.6): under `dedicated`, pinning the operator's checkout
            # would make every legitimate write into the run worktree read as a
            # path escape and the judge would deny the whole run.
            repo_root=self.work_root,
            run_dir=run_dir,  # where judge.json lands (§6.2) — gitignored
        )
        env = judge.start()
        try:
            return fn(env)
        finally:
            judge.stop()
            # The judge stopped, so its audit log is fully flushed — fold any
            # LLM-classifier spend it recorded into the manifest (review F-003).
            self._merge_judge_usage(man, run_dir)

    def _merge_judge_usage(self, man: Manifest, run_dir: Path) -> None:
        """Fold judge LLM-classifier spend into the manifest (review F-003).

        The judge runs as a separate process and records each LLM-rung
        decision's usage in ``judge-audit.jsonl``. Without this merge that spend
        never reaches ``manifest.totals``/``agent_usage``, so it is excluded from
        both total run cost and the per-profile table — and the FR-3 acceptance
        check ("judge/triage/retro each < 5% of total") cannot be measured.

        Idempotent: the ``judge_llm`` total is recomputed from the FULL audit on
        every call and only the delta is applied to ``totals``. A run that parks
        and resumes (or steps through several gates) appends to the same audit
        and re-runs this merge, so judge spend is never double counted.
        """
        from gauntlet.adapters.base import Usage

        audit_path = run_dir / "judge-audit.jsonl"
        if not audit_path.exists():
            return
        agg = M.UsageTotals()
        saw_usage = False
        for line in audit_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:  # a torn final line is not fatal here
                continue
            recorded = entry.get("usage")
            if not recorded:
                continue
            saw_usage = True
            agg.add(Usage(**recorded))
        if not saw_usage:
            return
        prior = man.agent_usage.get("judge_llm") or M.UsageTotals()
        delta = Usage(
            input_tokens=agg.input_tokens - prior.input_tokens,
            output_tokens=agg.output_tokens - prior.output_tokens,
            cached_input_tokens=agg.cached_input_tokens - prior.cached_input_tokens,
            cost_usd=(None if agg.cost_usd is None
                      else agg.cost_usd - (prior.cost_usd or 0.0)),
        )
        man.totals.add(delta)
        man.agent_usage["judge_llm"] = agg
        man.write_atomic(run_dir / "manifest.json")

    def _approve_drive(self, layout, run_dir, pipeline, man, gate, notes, env,
                       adapter_factory):
        orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env=env,
                                  adapter_factory=adapter_factory)
        status = orch.approve_gate(gate, notes)
        self._maybe_draft_pr(layout, run_dir, man, status)
        return status

    def _orchestrator(self, layout, run_dir, pipeline, man, *, judge_env,
                      adapter_factory=None, extra_context=None, clock=None,
                      response_action=None, interrupted_override=None) -> Orchestrator:
        kwargs = dict(
            repo_root=self.repo_root,
            # F-001: the whole phase turns on this line. Without it the
            # Orchestrator falls back to `work_root = repo_root`, so a
            # `dedicated` run's agents, shell steps and engine commits all
            # execute against the OPERATOR's checkout while the judge boundary
            # is pinned to the run worktree — silently defeating A1 with the
            # judge off, and denying every legitimate write as a path escape
            # with it on. Reads `self.work_root`, which `_run_paths` has
            # already resolved for this run (`repo_root` outside a drive).
            work_root=self.work_root,
            run_dir=run_dir,
            artifact_root=layout.slug_dir,
            config=self.config,
            pipeline=pipeline,
            manifest=man,
            writer=self.writer,
            judge_env=judge_env,
            adapter_factory=adapter_factory,
            extra_context=extra_context or {},
            response_action=response_action,
            interrupted_override=interrupted_override,
        )
        if clock is not None:
            kwargs["clock"] = clock
        return Orchestrator(**kwargs)

    # Every prompt-template reference a step can carry, so the manifest records
    # the exact version of the whole prompt set a run used (FR-5.6 / the P5
    # "versioned prompt set" deliverable) — not just the `prompt:` author/commit
    # templates, but the adversarial_cycle's review/triage/fix/confirm overrides.
    _PROMPT_REF_KEYS = (
        "prompt", "review_prompt", "rereview_prompt", "triage_prompt",
        "fix_prompt", "confirm_prompt",
        # retrospective + proposal-synthesis templates (FR-6.2/6.3): versioned
        # like every other prompt, so a retro proposal that edits them shows up
        # in the next run's manifest hashes (FR-6 acceptance).
        "retro_prompt", "synthesis_prompt",
    )

    def _prompt_hashes(self, pipeline) -> dict[str, str]:
        from gauntlet.engine.cycle import CYCLE_PROMPT_DEFAULTS
        from gauntlet.engine.pipeline import content_hash

        hashes: dict[str, str] = {}

        def record(ref: str | None) -> None:
            if ref and ref not in hashes:
                path = self.repo_root / self.config.asset_root / ref
                if path.exists():
                    hashes[ref] = content_hash(path.read_text())

        # Judge policy is a versioned, retro-tunable asset (FR-6.3): record its
        # content hash so an approved policy proposal provably changes the next
        # run's manifest, exactly as an approved prompt proposal does (FR-6
        # acceptance — "the next run uses the new version, visible in the
        # manifest's prompt/policy hashes").
        record("policy.yaml")

        for step in pipeline.all_steps():
            for key in self._PROMPT_REF_KEYS:
                record(step.get(key))
            # An adversarial_cycle loads default templates for every role the
            # pipeline leaves unspecified (rereview/triage/fix/confirm), and those
            # files steer behavior — so hash the EFFECTIVE path for each role,
            # override or default, not just the refs spelled out in the YAML
            # (review F-002; FR-5.6 reproducibility).
            if step.type == "adversarial_cycle":
                for key, default_ref in CYCLE_PROMPT_DEFAULTS.items():
                    record(step.get(key) or default_ref)
        return hashes


# --- rollback state transition + replay finisher (P3, plan §4.3 step 7) --------


def _rewind_manifest_state(man: Manifest, run_dir: Path, target: str) -> None:
    """Rewind the manifest to match the reset branch (review F-002).

    Drop commits after the target, and reset to `pending` EVERY step record
    (any type, any iteration) that executes after the target phase boundary
    in pipeline order — not just the steps that produced dropped commits.
    Otherwise a later resume skips work `git reset --hard` removed and the
    branch and manifest disagree (FR-9.9). Idempotent: a manifest already
    rewound to ``target`` is unchanged.

    Module-level (not a RunManager method) so the registered recovery-intent
    replay finisher can re-run it from a fresh process after a crash between
    the executor's apply and this persist.
    """
    keep: list = []
    for commit in man.commits:
        keep.append(commit)
        if commit.sha == target:
            break
    man.commits = keep
    target_step = keep[-1].step_id

    pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
    order = [s.id for s in pipeline.all_steps()]
    try:
        cutoff = order.index(target_step)
    except ValueError:  # pragma: no cover - defensive
        cutoff = len(order) - 1
    keep_ids = set(order[: cutoff + 1])
    for rec in man.steps:
        if rec.id not in keep_ids:
            rec.status = M.PENDING
            rec.base_sha = None
            rec.session_id = None
            rec.ended = None
    man.status = M.RUN_PARKED
    man.current_step = None


def _apply_rollback_manifest_transition(
    man: Manifest, run_dir: Path, *, target: str, phase: int, at: str
) -> None:
    """Rollback's step-7 state transition: manifest rewind + circuit breaker.

    Reversal circuit breaker (pipeline-effectiveness FR-4.2): rolling back
    past a phase boundary IS the human reversal of any auto-approved gate at
    or beyond it. Record the reversal on each such ``auto_approval`` and flip
    the run's effective auto-approval policy to ``always`` for the remainder
    of the run, so a later resume never re-auto-approves a gate the human
    just rolled back — a human has signalled distrust (§9).
    """
    from gauntlet.engine import gates

    _rewind_manifest_state(man, run_dir, target)
    reversed_n = gates.record_reversals(
        man, min_phase_num=phase, user="operator", at=at,
        notes=f"gauntlet rollback to phase P{phase} boundary",
    )
    if reversed_n:
        man.warnings.append(
            f"auto-approval disabled for the remainder of the run: {reversed_n} "
            f"auto-approved gate(s) reversed by rollback to P{phase} (FR-4.2)"
        )
    man.write_atomic(run_dir / "manifest.json")


def _rollback_replay_finisher(
    repo: Path, run_dir: Path, intent: "RX.RecoveryIntent"
) -> None:
    """Re-persist rollback's manifest transition after a replayed apply.

    Registered under the ``run.rollback`` site so a rollback killed between
    the executor's Git apply and the manifest persist converges FULLY on
    replay: the branch reset was already re-effected by the executor's replay;
    this re-runs the manifest rewind + circuit breaker idempotently. Without
    it, a replayed rollback would leave the branch behind the manifest — a
    state the rollback guards then refuse, wedging the run (fail closed is
    for uncertain states, not for ones the intent proves).
    """
    man = Manifest.load(run_dir / "manifest.json")
    target = intent.spec.target_sha
    if man.commits and man.commits[-1].sha == target:
        return  # already rewound: the crash landed after persist, before clear
    if not any(c.sha == target for c in man.commits):
        man.warnings.append(
            f"replayed rollback intent {intent.intent_id}: target "
            f"{target[:10]} is not a recorded commit; manifest left untouched "
            f"— reconcile manually (snapshot at {intent.snapshot_ref})"
        )
        man.write_atomic(run_dir / "manifest.json")
        return
    absorbed = intent.payload.get("absorbed")
    last_recorded = intent.payload.get("last_recorded") or ""
    if absorbed:
        n = len(str(absorbed).splitlines())
        man.warnings.append(
            f"rollback absorbed {n} unmanifested commit(s) above the last "
            f"recorded commit {str(last_recorded)[:10]} (branch was ahead of "
            f"the manifest); preserved in recovery snapshot "
            f"{intent.snapshot_ref}:\n{absorbed}"
        )
    man.warnings.append(
        f"rollback intent {intent.intent_id} was replayed after a process "
        f"death; snapshot retained at {intent.snapshot_ref}"
    )
    _apply_rollback_manifest_transition(
        man, run_dir, target=target, phase=int(intent.payload.get("phase", 0)),
        at=_utc_stamp(),
    )


RX.REPLAY_FINISHERS.setdefault("run.rollback", _rollback_replay_finisher)
