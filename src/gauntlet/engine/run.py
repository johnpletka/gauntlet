"""Run lifecycle: new / run / status / approve / reject / resume / abort / rollback.

Glue between the CLI and the :class:`Orchestrator`. Owns the on-disk layout
(FR-4.1), the entry contract (FR-10.1), branch management (FR-9.1), the
engine-managed judge lifecycle (FR-7.1), and guarded rollback (FR-9.9 /
review F-010).
"""

from __future__ import annotations

import atexit
import hmac
import json
import os
import secrets
import shutil
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.execution import run_bookkeeping_excludes
from gauntlet.engine.identity import resolve_operator_identity
from gauntlet.engine.judgeproc import ManagedJudge
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.orchestrator import Orchestrator, ResponseAction
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.validate import validate_pipeline
from gauntlet.logging.redact import RedactingWriter, build_redactor
from gauntlet.procident import (
    ProcessIdentity,
    read_process_identity,
)

# The worktree-scoped active-run lockfile name (FR-10.5). One per repo/worktree,
# at the resolved run root, gitignored.
DRIVING_LOCK_NAME = ".driving.lock"

# Console sidecar layout (also imported by web.jobproc so the two agree). The
# engine needs these to honour the run-id reservation handshake (FR-6.1a,
# review F-005): the console supervisor writes a single-use reservation token
# under `run_dir/.serve/` *before* launching this child, and `start()` accepts a
# pre-existing run dir only when it is exactly that fresh reservation.
SERVE_DIRNAME = ".serve"
RESERVATION_FILENAME = "reservation"

# Bounded retries for the acquire loop when racing a stale-lock reclaim, so a
# pathological churn raises rather than spins (fail closed).
_LOCK_ACQUIRE_RETRIES = 50

# Marker written into a scaffolded PRD; the entry contract refuses to run while
# it is still present (FR-10.1 / review OQ-1: existence + non-stub-ness).
PRD_STUB_MARKER = "<!-- GAUNTLET-PRD-STUB: replace this file with a real PRD -->"

_PRD_STUB = f"""{PRD_STUB_MARKER}
# PRD: <title>

> Gauntlet does not author PRDs (FR-10.1). Replace this stub with a real,
> human-authored PRD, then run `gauntlet run <slug>`. The run refuses to start
> while this marker is present.

## Problem statement

## Requirements
"""


class EntryContractError(RuntimeError):
    """The entry contract (FR-10.1) is not satisfied."""


class RollbackGuardError(RuntimeError):
    """A rollback guard (review F-010) refused the operation."""


class AbortGuardError(RuntimeError):
    """`abort()` refused because the target run is terminal (review F-002)."""


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


def _strip_marker(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if PRD_STUB_MARKER not in line)


def _normalize(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


@dataclass
class _LockRecord:
    """The on-disk content of ``<run_root>/.driving.lock`` (FR-10.5).

    ``nonce`` is a fresh per-acquisition random token; the holder keeps it in
    memory and releases the lock **only** if the file still carries that nonce
    (review F-004), so a holder that was already reclaimed-as-stale can never
    unlink a *new* owner's lock. ``proc_identity`` is the FR-7.2 OS
    process-creation identity (or ``None`` if unobtainable → unverifiable).
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
    def from_json(cls, text: str) -> "_LockRecord | None":
        try:
            data = json.loads(text)
            return cls(
                nonce=data["nonce"],
                slug=data["slug"],
                run_id=data.get("run_id"),
                pid=int(data["pid"]),
                pgid=int(data.get("pgid", data["pid"])),
                started_at=data.get("started_at", ""),
                host=data.get("host", ""),
                proc_identity=data.get("proc_identity"),
            )
        except (ValueError, KeyError, TypeError):
            return None


@dataclass
class _LockHandle:
    """An acquired worktree lock; carries the nonce that authorises release."""

    path: Path
    nonce: str


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
        return self.slug_dir / self.active_pointer.read_text().strip()


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
            layout.prd_path.write_text(_PRD_STUB)
        return layout.prd_path

    # ---- entry contract (FR-10.1) -------------------------------------------
    def check_entry_contract(self, slug: str) -> None:
        layout = self.layout(slug)
        if not layout.prd_path.exists():
            raise EntryContractError(
                f"{layout.prd_path} does not exist; `gauntlet new {slug}` scaffolds "
                "a stub for a human to author (FR-10.1)"
            )
        content = layout.prd_path.read_text()
        if PRD_STUB_MARKER in content:
            raise EntryContractError(
                f"{layout.prd_path} is still the scaffolded stub; a human must "
                "author the PRD before a run can start (FR-10.1)"
            )
        # Deleting only the marker line leaves the rest of the scaffold intact —
        # still not a human-authored PRD. Compare the whole body, marker-stripped
        # and whitespace-normalized, against the stub (review F-007).
        if _normalize(content) == _normalize(_strip_marker(_PRD_STUB)):
            raise EntryContractError(
                f"{layout.prd_path} is the scaffolded stub with only the marker "
                "removed; a human must author a real PRD before a run (FR-10.1)"
            )

    def _resolve_base_branch(self) -> str:
        """Resolve ``config.base_branch``, expanding the ``current`` sentinel.

        ``base_branch: current`` means "branch from whatever I'm on", so a run
        stacks on an integration branch without a per-run flag. Fail closed on a
        detached HEAD — there is no branch name to record or merge back into.
        """
        raw = (self.config.base_branch or "").strip()
        if raw.lower() in _BASE_CURRENT_SENTINELS:
            cur = gitops.current_branch(self.repo_root)
            if cur == "HEAD":
                raise EntryContractError(
                    "base_branch is 'current' but HEAD is detached; check out a "
                    "branch to run from before `gauntlet run`"
                )
            return cur
        return raw

    def _prepare_run_branch(self, branch: str, base: str) -> None:
        """Put the worktree on a clean run branch ``branch`` based on ``base``.

        Fail-closed branch lifecycle (replaces a bare ``checkout``, which once
        silently rewound a worktree onto a stale branch):

        * absent            -> create it off ``base``.
        * merged into base  -> spent; discard and recreate fresh off ``base``.
          (After ``finish``/merge into the base, re-running the slug self-heals.)
        * unmerged/divergent -> REFUSE. The branch carries commits not in
          ``base``; adopting it could rewind the tree or stack on stale work.
          The human resolves it (`gauntlet clean`, merge, or rename).
        """
        repo = self.repo_root
        if not gitops.branch_exists(repo, branch):
            gitops.checkout_or_create_branch(repo, branch, base)
            return
        if gitops.is_ancestor(repo, branch, base):
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

    def _run_root_dir(self) -> Path:
        return self.repo_root / self.config.run_root

    def _lock_path(self) -> Path:
        return self._run_root_dir() / DRIVING_LOCK_NAME

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

    def _read_lock(self) -> _LockRecord | None:
        try:
            text = self._lock_path().read_text()
        except (OSError, FileNotFoundError):
            return None
        return _LockRecord.from_json(text)

    @staticmethod
    def _lock_is_live(rec: _LockRecord) -> bool:
        """True unless the lock's holder is *proven* gone (FR-10.5).

        A worktree lock must fail **closed**: reclaim only when we can prove the
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

    @staticmethod
    def _lock_busy_message(rec: _LockRecord) -> str:
        who = f"{rec.slug}/{rec.run_id}" if rec.run_id else rec.slug
        return (
            f"worktree is being driven by {who} (pid {rec.pid}); wait, or "
            "abort that run first (FR-10.5)"
        )

    def _new_lock_record(self, slug: str, run_id: str | None) -> _LockRecord:
        pid = os.getpid()
        try:
            pgid = os.getpgid(pid)
        except OSError:  # pragma: no cover - platform without process groups
            pgid = pid
        identity = read_process_identity(pid)
        return _LockRecord(
            nonce=secrets.token_hex(16),
            slug=slug,
            run_id=run_id,
            pid=pid,
            pgid=pgid,
            started_at=_utc_stamp(),
            host=socket.gethostname(),
            proc_identity=identity.to_dict() if identity is not None else None,
        )

    @staticmethod
    def _link_into_place(lock_path: Path, nonce: str, payload: str) -> bool:
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

    def _try_reclaim(
        self, lock_path: Path, observed: _LockRecord | None, nonce: str, payload: str
    ) -> bool:
        """Best-effort reclaim of a stale/corrupt lock; True iff we now hold it.

        Re-reads the lock immediately before removing it and unlinks **only** the
        record we observed as stale (matching nonce) — never a *new* owner's
        fresh lock (the F-004 inverse of ownership-validated release). Then races
        to atomically link our record into place; a lost race (someone else
        reclaimed first) returns ``False`` so the caller re-evaluates the holder.
        """
        current = self._read_lock()
        if current is not None:
            if self._lock_is_live(current):
                return False  # became live (or a fresh owner) → caller fails closed
            if observed is None or current.nonce != observed.nonce:
                return False  # changed under us → re-evaluate, don't blind-unlink
        # current is None (corrupt/vanished) or matches our observed stale record:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass
        return self._link_into_place(lock_path, nonce, payload)

    def _acquire_worktree_lock(
        self, slug: str, run_id: str | None
    ) -> _LockHandle:
        """Acquire the worktree lock or fail closed (FR-10.5).

        Atomic create-if-absent via ``os.link`` so check-and-acquire has no
        TOCTOU window and the lock is never observed empty. A lock held by a
        **live** pid fails the verb closed regardless of slug; a
        dead/reused/unverifiable lock is reclaimed as stale. Acquired **first**
        by `start`/`resume`/`approve`, before any run dir / `active-run.txt` /
        git mutation.
        """
        run_root = self._run_root_dir()
        run_root.mkdir(parents=True, exist_ok=True)
        self._ensure_run_root_gitignore(run_root)
        lock_path = self._lock_path()
        record = self._new_lock_record(slug, run_id)
        payload = record.to_json()
        for _ in range(_LOCK_ACQUIRE_RETRIES):
            if self._link_into_place(lock_path, record.nonce, payload):
                return self._take_handle(lock_path, record.nonce)
            existing = self._read_lock()
            if existing is not None and self._lock_is_live(existing):
                raise WorktreeLockError(self._lock_busy_message(existing))
            if self._try_reclaim(lock_path, existing, record.nonce, payload):
                return self._take_handle(lock_path, record.nonce)
            # transient race (a concurrent reclaim/empty window) → re-evaluate
        raise WorktreeLockError(
            "could not acquire the worktree lock after repeated reclaim races "
            f"({lock_path}); a driver may be churning — fail closed (FR-10.5)"
        )

    def _take_handle(self, lock_path: Path, nonce: str) -> _LockHandle:
        handle = _LockHandle(path=lock_path, nonce=nonce)
        self._held_lock = handle
        atexit.register(self._release_worktree_lock, handle)
        return handle

    def _release_worktree_lock(self, handle: _LockHandle | None) -> None:
        """Release the lock, but only if it still carries our nonce (F-004).

        If the file now holds a different nonce, we were already reclaimed as
        stale and a *new* owner is driving — unlinking would re-open
        double-driving, so the release is a **no-op**. Idempotent: safe to call
        from the per-verb ``finally`` and again from the atexit fallback.
        """
        if handle is None:
            return
        current = self._read_lock()
        if current is not None and current.nonce == handle.nonce:
            try:
                os.unlink(handle.path)
            except FileNotFoundError:
                pass
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

        # Acquire the worktree lock FIRST — before any run dir / active-run.txt
        # / git mutation (FR-10.5). Released in `finally` on park/done/error.
        handle = self._acquire_worktree_lock(slug, run_id)
        try:
            self._refuse_if_active_run(layout)
            pipeline, phash = load_pipeline(pipeline_path)
            validate_pipeline(pipeline, self.config)

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
            self._prepare_run_branch(branch, base_branch)

            run_dir = layout.run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
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
            )
            return self._drive(
                layout, run_dir, pipeline, man,
                use_judge=use_judge, adapter_factory=adapter_factory,
                extra_context=extra_context, clock=clock,
            )
        finally:
            self._release_worktree_lock(handle)

    # ---- resume (FR-8.2) ----------------------------------------------------
    def resume(self, slug: str, *, response: str | None = None,
               use_judge: bool = True, adapter_factory=None,
               extra_context: dict | None = None, clock=None) -> str:
        layout = self.layout(slug)
        self._ensure_slug_gitignore(layout)  # idempotent (#33; old runs too)
        run_dir = layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")
        # Resume is a driving verb (FR-10.5): take the worktree lock FIRST,
        # before the branch checkout / drive. The lock record carries this run's
        # id from the manifest so a concurrent verb's refusal names the holder.
        handle = self._acquire_worktree_lock(slug, man.run_id)
        try:
            pipeline, phash = load_pipeline(run_dir / "pipeline.yaml")
            if phash != man.pipeline.hash:
                raise RuntimeError(
                    "pipeline content hash changed since the run started "
                    f"({man.pipeline.hash} -> {phash}); resume refuses to run a "
                    "different pipeline against an existing manifest (FR-5.6)"
                )
            # F-1: resume continues the SAME branch the run committed to. Never
            # recreate it from base (the old checkout_or_create_branch) — that
            # would silently drop the manifest's recorded commits. Fail closed if
            # the branch is gone or its tip no longer contains every recorded
            # commit (reset / recreated / divergent), like rollback's guard.
            repo = self.repo_root
            if not gitops.branch_exists(repo, man.branch):
                raise RunBranchStateError(
                    f"resume: run branch {man.branch!r} is missing; recreating "
                    "it from base would drop the manifest's recorded commits. "
                    "Restore the branch (e.g. from refs/gauntlet/backup/) first."
                )
            # Validate the branch REF *before* checking it out — checking out
            # first would rewind the worktree onto a stale/reset branch even
            # though we are about to refuse. The last recorded commit must be
            # reachable from the branch tip: tip == last (normal interrupt) or
            # slightly ahead (killed between commit and manifest persist) is
            # fine; behind/divergent means recorded commits are missing.
            if man.commits:
                last = man.commits[-1].sha
                if not gitops.is_ancestor(repo, last, man.branch):
                    raise RunBranchStateError(
                        f"resume: branch {man.branch!r} is missing the "
                        f"manifest's recorded commit {last[:10]} (reset or "
                        "recreated); the branch and manifest disagree. "
                        "Reconcile (restore the branch, or `gauntlet rollback`) "
                        "before resuming."
                    )
            gitops.checkout_branch(repo, man.branch)
            # Plan the --response transition (FR-1/FR-1.1/FR-8/FR-9 guards +
            # FR-7.1 idempotent recovery). All validation and operator-identity
            # resolution happen HERE, before driving; the orchestrator only
            # applies an already-validated, fail-closed decision.
            action = self._plan_response_action(man, response)
            return self._drive(
                layout, run_dir, pipeline, man,
                use_judge=use_judge, adapter_factory=adapter_factory,
                extra_context=extra_context, clock=clock,
                response_action=action,
            )
        finally:
            self._release_worktree_lock(handle)

    def _plan_response_action(
        self, man: Manifest, response: str | None
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
            if (
                parked is not None
                and parked.parked_reason in M.RESPONSE_RESOLVABLE_PARK_REASONS
            ):
                what = (
                    "an upstream conflict"
                    if parked.parked_reason == M.PARKED_REASON_UPSTREAM_CONFLICT
                    else "a cycle escalation its own loop cannot resolve"
                )
                raise ValueError(
                    f"step '{parked.id}' parked on {what}; resume it with "
                    '--response "<decision>" (see `gauntlet resume --help`). '
                    "Re-running without a decision would only re-surface it."
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
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")
        gate = gate or man.current_step
        if gate is None:
            raise ValueError("no gate to approve; run is not parked")
        # Approve drives the rest of the run, so it is a driving verb (FR-10.5):
        # take the worktree lock first, released in `finally` on the next park /
        # done / error.
        handle = self._acquire_worktree_lock(slug, man.run_id)
        try:
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

    def reject(self, slug: str, notes: str, gate: str | None = None) -> str:
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")
        gate = gate or man.current_step
        if gate is None:
            raise ValueError("no gate to reject; run is not parked")
        pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
        orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env={})
        return orch.reject_gate(gate, notes)

    # ---- abort --------------------------------------------------------------
    def abort(self, slug: str) -> str:
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
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
        return man.status

    # ---- clean (run-branch tidy) --------------------------------------------
    def clean(self, slug: str, *, force: bool = False) -> str:
        """Delete the run branch once it is merged; preserve the run record.

        Safe by construction: refuse unless ``gauntlet/<slug>`` is fully merged
        into its recorded base (``--force`` overrides). Removes only the
        ephemeral branch + the live ``active-run.txt`` pointer — never the
        committed run dir (prd.md, manifest, transcripts are the audit trail).
        """
        layout = self.layout(slug)
        repo = self.repo_root
        branch = f"{self.config.branch_prefix}{slug}"
        if not gitops.branch_exists(repo, branch):
            cleared = self._clear_active_pointer(layout)
            return (
                f"no branch {branch!r}"
                + ("; cleared stale active-run pointer" if cleared else "; nothing to do")
            )
        base = self._recorded_base(layout)
        if not force:
            if base is None:
                raise RunBranchNotMergedError(
                    f"cannot determine the base for {branch!r} (no run manifest); "
                    "merge it and retry, or pass --force to delete anyway"
                )
            if not gitops.is_ancestor(repo, branch, base):
                raise RunBranchNotMergedError(
                    f"refusing to delete {branch!r}: not fully merged into base "
                    f"{base!r}. Merge it first (e.g. `gauntlet finish {slug}`), "
                    "or pass --force to discard it."
                )
        if gitops.current_branch(repo) == branch:
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
                repo, layout.active_run_dir(), layout.slug_dir
            )
            if not gitops.is_clean(repo, exclude=excludes):
                raise WorktreeDirtyError(
                    f"refusing clean: worktree is dirty and clean must step off "
                    f"{branch!r} onto {target!r}, which would carry the changes "
                    "onto the base. Commit or discard them first."
                )
            gitops.checkout_branch(repo, target)
        gitops.delete_branch(repo, branch)
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
        man = Manifest.load(run_dir / "manifest.json")
        repo = self.repo_root
        branch, base = man.branch, man.base_branch

        if man.status != M.RUN_DONE:
            raise FinishError(
                f"run {man.run_id!r} is {man.status!r}, not done; finish merges "
                "only a completed run — resume or approve its gates first"
            )
        excludes = run_bookkeeping_excludes(self.repo_root, run_dir, layout.slug_dir)
        if not gitops.is_clean(repo, exclude=excludes):
            raise FinishError(
                "refusing finish: worktree is dirty; commit or discard first"
            )
        if not gitops.branch_exists(repo, branch):
            raise FinishError(f"run branch {branch!r} does not exist")
        if not gitops.branch_exists(repo, base):
            raise FinishError(f"base branch {base!r} does not exist")

        # Already merged (e.g. landed via a PR): nothing to merge, just tidy.
        if gitops.is_ancestor(repo, branch, base):
            if gitops.current_branch(repo) == branch:
                gitops.checkout_branch(repo, base)
            gitops.delete_branch(repo, branch)
            self._clear_active_pointer(layout)
            return f"already merged into {base!r}; deleted {branch!r}"

        gitops.checkout_branch(repo, base)
        msg = f"Merge {branch} into {base} (gauntlet finish {slug}, run {man.run_id})"
        try:
            gitops.merge_branch(repo, branch, message=msg)
        except gitops.GitError as exc:
            gitops.merge_abort(repo)
            gitops.checkout_branch(repo, branch)  # leave the human where they were
            raise FinishError(
                f"merge of {branch!r} into {base!r} conflicts; resolve it "
                f"manually (merge aborted, back on {branch!r}). Details: {exc}"
            )
        gitops.delete_branch(repo, branch)
        self._clear_active_pointer(layout)
        return f"merged {branch!r} into {base!r} and deleted the branch"

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

    # ---- status -------------------------------------------------------------
    def status(self, slug: str) -> Manifest:
        layout = self.layout(slug)
        return Manifest.load(layout.active_run_dir() / "manifest.json")

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
            if not gitops.is_clean(self.repo_root, exclude=excludes):
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
        man = Manifest.load(run_dir / "manifest.json")

        # Guard 1: clean work tree — only the engine's own bookkeeping is
        # excluded (review F-001), so an uncommitted real artifact still blocks.
        excludes = run_bookkeeping_excludes(self.repo_root, run_dir, layout.slug_dir)
        if not gitops.is_clean(self.repo_root, exclude=excludes):
            raise RollbackGuardError(
                "refusing rollback: worktree is dirty; commit or discard first"
            )
        # Guard 2: branch tip MUST equal the manifest's last recorded commit.
        # A branch ahead of the manifest (extra unmanifested commits) is a
        # divergence — reset would silently discard those commits (review F-003).
        if not man.commits:
            raise RollbackGuardError("no recorded commits to roll back to")
        last_recorded = man.commits[-1].sha
        head = gitops.head_sha(self.repo_root)
        if head != last_recorded:
            raise RollbackGuardError(
                "refusing rollback: branch has diverged from the manifest "
                f"(HEAD {head[:10]} != last recorded {last_recorded[:10]}); the "
                "branch and manifest must agree before a rewind (FR-9.9)"
            )
        # Resolve the target: the last commit whose phase prefix is P<phase>.
        target = self._phase_boundary_sha(man, phase)
        if target is None:
            raise RollbackGuardError(
                f"no recorded phase-{phase} commit boundary to roll back to"
            )

        # Backup ref + manifest snapshot before any rewind (F-010).
        ts = _utc_stamp()
        gitops.create_ref(
            self.repo_root, f"refs/gauntlet/backup/{man.run_id}/{ts}", head
        )
        shutil.copy2(run_dir / "manifest.json", run_dir / f"manifest.snapshot-{ts}.json")

        gitops.reset_hard(self.repo_root, target)
        self._rewind_manifest(man, run_dir, target)
        man.write_atomic(run_dir / "manifest.json")
        return target

    def _rewind_manifest(self, man: Manifest, run_dir: Path, target: str) -> None:
        """Rewind the manifest to match the reset branch (review F-002).

        Drop commits after the target, and reset to `pending` EVERY step record
        (any type, any iteration) that executes after the target phase boundary
        in pipeline order — not just the steps that produced dropped commits.
        Otherwise a later resume skips work `git reset --hard` removed and the
        branch and manifest disagree (FR-9.9).
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
               extra_context, clock, response_action=None) -> str:
        if not use_judge:
            orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env={},
                                      adapter_factory=adapter_factory,
                                      extra_context=extra_context, clock=clock,
                                      response_action=response_action)
            status = orch.drive()
        else:
            status = self._with_judge(man, run_dir, lambda env: self._orchestrator(
                layout, run_dir, pipeline, man, judge_env=env,
                adapter_factory=adapter_factory, extra_context=extra_context,
                clock=clock, response_action=response_action).drive())
        self._maybe_draft_pr(layout, run_dir, man, status)
        return status

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
            repo_root=self.repo_root,  # the fixed path boundary (notes #29)
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
                      response_action=None) -> Orchestrator:
        kwargs = dict(
            repo_root=self.repo_root,
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
