"""Lossless Git recovery snapshots (recovery-redesign P2, plan §4.4).

A :class:`GitRecoverySnapshot` preserves every independent Git state plane —
checked-out branch and HEAD, run-branch ref, raw index bytes (including
unmerged conflict stages), a ``write-tree`` index tree when one is derivable,
and a worktree tree built through a *temporary* index — and anchors all of it
under one durable ref::

    refs/gauntlet/recovery/<run_id>/<snapshot_id>

Reachability is structural: the snapshot commit's tree contains the raw-index
blob, the worktree tree, the index tree, and the serialized metadata, and its
parents are the original HEAD (and run-branch tip, when distinct). Everything
needed for exact restoration is therefore reachable from the single ref
through Git's object graph — never merely named in a commit message.

Invariants (plan §2):

* **R3 — creation is observational.** Creating a snapshot never changes HEAD,
  any branch ref, the real index (not even its cache-tree bytes: ``write-tree``
  runs against a byte copy), the working tree, staging state, or file types.
* **R2 — preserve before mutation.** Callers create the snapshot *before*
  checkout/reset/clean; a snapshot failure therefore precedes any destructive
  step and fails the operation closed.
* Restoration uses Git plumbing/materialization (``read-tree`` into a
  temporary index + ``checkout-index``), never ``Path.write_bytes`` through a
  worktree path, and is idempotent.

P2 migrates exactly one pilot rewind path (the orchestrator's conflict-park
clean restore) onto this mechanism; the remaining rewind paths and the
``RecoveryExecutor`` transaction follow in P3.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gauntlet.engine import gitops
from gauntlet.engine.gitops import ENGINE_IDENTITY, GitError, _exclude_pathspec

RECOVERY_REF_PREFIX = "refs/gauntlet/recovery"
SNAPSHOT_SCHEMA_VERSION = 1

# Fixed entry names inside the snapshot wrapper tree. Engine constants, never
# model- or user-derived.
METADATA_ENTRY = "metadata.json"
RAW_INDEX_ENTRY = "index.raw"
WORKTREE_ENTRY = "worktree"
INDEX_TREE_ENTRY = "index"

_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
# One ref path component: conservative charset, no leading separator noise.
_REF_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SnapshotError(RuntimeError):
    """A recovery snapshot could not be created, loaded, or restored."""


def _validate_sha(value: str, *, field: str) -> None:
    if not _SHA_RE.fullmatch(value):
        raise ValueError(f"{field} must be a full 40- or 64-hex object id")


def _validate_rel_path(value: str, *, field: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value == "."
        or path.parts[0] == ".git"
    ):
        raise ValueError(f"{field} must be a contained repository-relative path")


def _validate_ref_component(value: str, *, field: str) -> None:
    if not _REF_COMPONENT_RE.fullmatch(value) or value.endswith(".lock"):
        raise ValueError(
            f"{field} {value!r} is not a safe single ref path component"
        )


class GitRecoverySnapshot(BaseModel):
    """Metadata record of one complete recovery snapshot (plan §4.4).

    The same shape is serialized as ``metadata.json`` inside the snapshot
    commit's tree (minus ``snapshot_commit``, which *is* that commit and
    cannot be known before it exists), so a crash after ref creation loses
    nothing: :func:`load_snapshot` rebuilds this record from the ref alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    snapshot_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt_id: str | None = None
    reason: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    checked_out_branch: str | None
    head_sha: str
    run_branch: str | None
    run_branch_sha: str | None
    # Raw real-index bytes stored as a blob — preserves unmerged stages that
    # ``write-tree`` cannot represent. ``None`` only when no index file existed.
    raw_index_oid: str | None
    # The normal ``write-tree`` of (a byte copy of) the index; ``None`` when
    # write-tree fails (unmerged index) or no index file existed.
    index_tree: str | None
    # The working tree staged through a temporary index seeded from HEAD.
    worktree_tree: str
    # Concrete protected paths present in the worktree tree (force-included
    # even when gitignored), and protected paths deleted vs HEAD whose
    # deletion a restore must reproduce (a reset to HEAD resurrects them).
    protected_paths: tuple[str, ...] = ()
    protected_deletions: tuple[str, ...] = ()
    # Pathspec exclusions applied at creation (active engine state). A restore
    # must not delete files under these: they were deliberately not captured.
    excluded_paths: tuple[str, ...] = ()
    ref: str = Field(min_length=1)
    snapshot_commit: str

    @model_validator(mode="after")
    def _contract(self) -> "GitRecoverySnapshot":
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported snapshot schema_version {self.schema_version}"
            )
        _validate_ref_component(self.snapshot_id, field="snapshot_id")
        _validate_ref_component(self.run_id, field="run_id")
        _validate_sha(self.head_sha, field="head_sha")
        _validate_sha(self.worktree_tree, field="worktree_tree")
        _validate_sha(self.snapshot_commit, field="snapshot_commit")
        if self.run_branch_sha is not None:
            _validate_sha(self.run_branch_sha, field="run_branch_sha")
        if self.raw_index_oid is not None:
            _validate_sha(self.raw_index_oid, field="raw_index_oid")
        if self.index_tree is not None:
            _validate_sha(self.index_tree, field="index_tree")
        expected_ref = f"{RECOVERY_REF_PREFIX}/{self.run_id}/{self.snapshot_id}"
        if self.ref != expected_ref:
            raise ValueError(
                f"ref {self.ref!r} does not match its run/snapshot ids "
                f"({expected_ref!r})"
            )
        for field_name in ("protected_paths", "protected_deletions", "excluded_paths"):
            for rel in getattr(self, field_name):
                _validate_rel_path(rel, field=field_name)
        return self

    def metadata_json(self) -> str:
        """The serialized form embedded in the snapshot tree.

        Excludes ``snapshot_commit`` (the commit that will carry these bytes)
        — :func:`load_snapshot` re-derives it from the resolved ref.
        """
        data = self.model_dump(mode="json", exclude={"snapshot_commit"})
        return json.dumps(data, sort_keys=True, indent=2) + "\n"


@contextmanager
def _temp_workspace() -> Iterator[Path]:
    """A throwaway directory OUTSIDE the repository for temporary index files.

    Living outside the worktree/git dir is what lets
    ``gitops.validate_temp_index_path`` accept these paths; it also guarantees
    the scratch files can never register as worktree dirt. Removed on success
    and on failure.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="gauntlet-recovery-"))
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _split_z(out: str) -> list[str]:
    return [part for part in out.split("\0") if part]


def snapshot_ref(run_id: str, snapshot_id: str) -> str:
    _validate_ref_component(run_id, field="run_id")
    _validate_ref_component(snapshot_id, field="snapshot_id")
    return f"{RECOVERY_REF_PREFIX}/{run_id}/{snapshot_id}"


def create_snapshot(
    repo: Path,
    *,
    run_id: str,
    reason: str,
    snapshot_id: str | None = None,
    attempt_id: str | None = None,
    run_branch: str | None = None,
    exclude: list[str] | None = None,
    protected: list[str] | None = None,
    created_at: str | None = None,
) -> GitRecoverySnapshot:
    """Create a complete, durable recovery snapshot. Observational (R3).

    Object layout: a wrapper tree holds ``metadata.json`` (this record),
    ``index.raw`` (the raw real-index bytes as a blob), ``worktree`` (the
    working tree staged through a temporary index), and ``index`` (the normal
    index tree, when ``write-tree`` succeeds on a byte copy). One commit
    carries that tree with the original HEAD — and the run-branch tip, when it
    differs — as parents, and ``refs/gauntlet/recovery/<run>/<snapshot>`` is
    created atomically only after every object exists.

    ``exclude`` (active engine state, e.g. the run root) is left out of the
    worktree tree; ``protected`` patterns (human-owned, possibly gitignored
    paths a subsequent rewind can affect) are force-included past both the
    exclusion and any gitignore rule, and their concrete matches — including
    deletions vs HEAD — are recorded for scoped restoration.

    Nothing here touches HEAD, any branch ref, the real index, or the working
    tree; a failure at any point leaves the repository exactly as observed
    (already-written loose objects are unreachable garbage, not mutations).
    """
    if created_at is None:
        created_at = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
    if snapshot_id is None:
        snapshot_id = "snapshot-" + re.sub(r"[^A-Za-z0-9._-]", "-", created_at)
    ref = snapshot_ref(run_id, snapshot_id)
    # Defense in depth: our component regex is stricter than git's rules, but
    # let git veto anything it would reject before we build objects for it.
    gitops._run(repo, "check-ref-format", ref)
    if _ref_exists(repo, ref):
        raise SnapshotError(f"recovery snapshot ref already exists: {ref}")

    head = gitops.head_sha(repo)
    branch = gitops.current_branch(repo)
    checked_out_branch = None if branch == "HEAD" else branch
    run_branch_sha: str | None = None
    if run_branch is not None and gitops.branch_exists(repo, run_branch):
        run_branch_sha = gitops.rev_parse(repo, f"refs/heads/{run_branch}")

    index_path = gitops.git_index_path(repo)
    raw_index_bytes = index_path.read_bytes() if index_path.exists() else None
    raw_index_oid = (
        gitops.hash_object_write(repo, raw_index_bytes)
        if raw_index_bytes is not None
        else None
    )

    exclude = list(exclude or [])
    protected = list(protected or [])

    with _temp_workspace() as tmpdir:
        # Normal index tree, from a BYTE COPY of the real index: ``write-tree``
        # rewrites its index's cache-tree extension, so running it against the
        # real index would change the real index bytes — an R3 violation.
        index_tree: str | None = None
        if raw_index_bytes is not None:
            index_copy = tmpdir / "index-copy"
            index_copy.write_bytes(raw_index_bytes)
            try:
                index_tree = gitops.run_with_temp_index(
                    repo, index_copy, "write-tree"
                ).strip()
            except GitError:
                index_tree = None  # unmerged index: raw bytes carry the truth

        # Worktree tree through a fresh temporary index: seed from HEAD (so
        # excluded paths keep their committed versions and the tree stays
        # complete), then let Git itself stage the live worktree — Git records
        # symlinks as symlinks and preserves executable modes, and gitignore
        # rules apply except where ``protected`` overrides them.
        work_index = tmpdir / "index-worktree"
        gitops.run_with_temp_index(repo, work_index, "read-tree", head)
        gitops.run_with_temp_index(
            repo, work_index, "add", "-A", *_exclude_pathspec(exclude)
        )
        if protected and _patterns_match_temp_index(repo, work_index, protected):
            gitops.run_with_temp_index(
                repo, work_index, "add", "-A", "-f", "--", *protected
            )
        worktree_tree = gitops.run_with_temp_index(
            repo, work_index, "write-tree"
        ).strip()

        protected_paths: tuple[str, ...] = ()
        protected_deletions: tuple[str, ...] = ()
        if protected:
            protected_paths = tuple(
                sorted(
                    _split_z(
                        gitops.run_with_temp_index(
                            repo, work_index, "ls-files", "-z", "--", *protected
                        )
                    )
                )
            )
            # Protected paths present in HEAD but absent from the worktree
            # tree: a later ``reset --hard HEAD`` resurrects them, so a scoped
            # restore must know to delete them again.
            protected_deletions = tuple(
                sorted(
                    _split_z(
                        gitops._run(
                            repo,
                            "diff-tree", "-r", "-z", "--name-only",
                            "--diff-filter=D", head, worktree_tree,
                            "--", *protected,
                        )
                    )
                )
            )

    record = GitRecoverySnapshot(
        snapshot_id=snapshot_id,
        run_id=run_id,
        attempt_id=attempt_id,
        reason=reason,
        created_at=created_at,
        checked_out_branch=checked_out_branch,
        head_sha=head,
        run_branch=run_branch,
        run_branch_sha=run_branch_sha,
        raw_index_oid=raw_index_oid,
        index_tree=index_tree,
        worktree_tree=worktree_tree,
        protected_paths=protected_paths,
        protected_deletions=protected_deletions,
        excluded_paths=tuple(exclude),
        ref=ref,
        # Placeholder until the commit exists; replaced below. Validated shape
        # only — never serialized (metadata_json excludes it).
        snapshot_commit=head,
    )

    metadata_oid = gitops.hash_object_write(
        repo, record.metadata_json().encode("utf-8")
    )
    entries = [
        f"100644 blob {metadata_oid}\t{METADATA_ENTRY}",
        f"040000 tree {worktree_tree}\t{WORKTREE_ENTRY}",
    ]
    if raw_index_oid is not None:
        entries.append(f"100644 blob {raw_index_oid}\t{RAW_INDEX_ENTRY}")
    if index_tree is not None:
        entries.append(f"040000 tree {index_tree}\t{INDEX_TREE_ENTRY}")
    wrapper_tree = gitops.mktree(repo, entries)

    parents = [head]
    if run_branch_sha is not None and run_branch_sha != head:
        parents.append(run_branch_sha)
    message = (
        f"gauntlet: recovery snapshot {snapshot_id} for run {run_id}\n\n"
        f"reason: {reason}\n"
        f"created_at: {created_at}\n"
        "Complete-format recovery snapshot (schema v"
        f"{SNAPSHOT_SCHEMA_VERSION}); see metadata.json in this commit's "
        "tree. Distinct from legacy refs/gauntlet/backup/ single-tree "
        "backups.\n"
    )
    commit = gitops.commit_tree(
        repo, wrapper_tree, parents, message, identity=ENGINE_IDENTITY
    )
    # Atomic durability point: the ref appears only after every required
    # object exists. A crash before this line leaves zero mutations; a crash
    # after it leaves everything needed for restoration reachable.
    gitops.create_ref(repo, ref, commit)
    return record.model_copy(update={"snapshot_commit": commit})


def _ref_exists(repo: Path, ref: str) -> bool:
    try:
        gitops._run(repo, "rev-parse", "--verify", "--quiet", ref)
        return True
    except GitError:
        return False


def _patterns_match_temp_index(
    repo: Path, work_index: Path, patterns: list[str]
) -> bool:
    """True iff ``patterns`` still match anything the scoped force-add can act on.

    ``git add`` errors on a pathspec that matches nothing, so the protected
    force-include is gated on an actual match *in the temporary index's view*:
    the general ``add -A`` may already have staged a protected deletion (the
    entry is then gone from both the temp index and the worktree, and a scoped
    add would error), while a protected path hidden by the exclusion pathspec
    still sits in the HEAD-seeded temp index awaiting its update. ``ls-files``
    exits zero with empty output on no match, making it the safe probe;
    ``--others`` without ``--exclude-standard`` deliberately includes
    gitignored files.
    """
    in_index = gitops.run_with_temp_index(
        repo, work_index, "ls-files", "-z", "--", *patterns
    )
    if in_index:
        return True
    others = gitops.run_with_temp_index(
        repo, work_index, "ls-files", "-z", "--others", "--", *patterns
    )
    return bool(others)


def load_snapshot(repo: Path, ref: str) -> GitRecoverySnapshot:
    """Rebuild a snapshot record from its durable ref (crash recovery).

    Fails closed: the metadata must parse and validate, its ``ref`` field must
    match the ref it was loaded from, and every object required for
    restoration must exist in the object database.
    """
    try:
        commit = gitops.rev_parse(repo, ref)
    except GitError as exc:
        raise SnapshotError(f"recovery snapshot ref not found: {ref}") from exc
    metadata_text = gitops.file_at_commit(repo, commit, METADATA_ENTRY)
    if metadata_text is None:
        raise SnapshotError(f"snapshot {ref} carries no {METADATA_ENTRY}")
    try:
        data = json.loads(metadata_text)
        record = GitRecoverySnapshot(**data, snapshot_commit=commit)
    except (ValueError, TypeError) as exc:
        raise SnapshotError(f"snapshot {ref} metadata is invalid: {exc}") from exc
    if record.ref != ref:
        raise SnapshotError(
            f"snapshot metadata names ref {record.ref!r} but was loaded from "
            f"{ref!r}; refusing a moved or renamed snapshot"
        )
    required = [record.worktree_tree]
    if record.raw_index_oid is not None:
        required.append(record.raw_index_oid)
    if record.index_tree is not None:
        required.append(record.index_tree)
    for oid in required:
        if not gitops.object_exists(repo, oid):
            raise SnapshotError(f"snapshot {ref} is missing object {oid}")
    return record


# --- restoration ---------------------------------------------------------------


def restore_snapshot(
    repo: Path, snapshot: GitRecoverySnapshot, *, exact_index: bool = True
) -> None:
    """Restore the full captured worktree state; idempotent.

    Materialization goes through Git (``read-tree`` into a temporary index,
    then ``checkout-index``) so symlinks, executable modes, and entry kinds
    are recreated by Git itself — never by writing bytes through a worktree
    path. Files present now but absent from the snapshot (and not gitignored,
    not under the creation-time exclusions) are removed, reproducing captured
    deletions exactly.

    With ``exact_index`` (the default) the raw real-index bytes are restored
    LAST, bringing back staged-vs-worktree divergence and unmerged conflict
    stages precisely as captured.
    """
    _require_objects(repo, snapshot)
    with _temp_workspace() as tmpdir:
        work_index = tmpdir / "index-restore"
        gitops.run_with_temp_index(
            repo, work_index, "read-tree", snapshot.worktree_tree
        )
        # Remove what the snapshot says should not exist: every non-ignored
        # file absent from the snapshot tree (skipping the creation-time
        # exclusions — those planes were deliberately not captured), plus the
        # recorded protected deletions (which hide under those exclusions).
        extraneous = _split_z(
            gitops.run_with_temp_index(
                repo, work_index, "ls-files", "-z", "--others",
                "--exclude-standard",
                *_exclude_pathspec(list(snapshot.excluded_paths)),
            )
        )
        for rel in extraneous:
            _safe_unlink(repo, rel)
        for rel in snapshot.protected_deletions:
            _safe_unlink(repo, rel)
        tree_paths = _split_z(
            gitops.run_with_temp_index(repo, work_index, "ls-files", "-z")
        )
        for rel in tree_paths:
            _prepare_destination(repo, rel)
        gitops.run_with_temp_index(repo, work_index, "checkout-index", "-f", "-a")
    if exact_index:
        _restore_raw_index(repo, snapshot)


def restore_protected(repo: Path, snapshot: GitRecoverySnapshot) -> None:
    """Restore ONLY the snapshot's protected entries; idempotent.

    The scoped counterpart used after a sanctioned rewind (e.g. the pilot
    conflict-park restore): the rewind itself is allowed to discard the
    captured implementation state, but human-owned protected paths must come
    back exactly — modified bytes, untracked/ignored files, and deletions a
    reset just resurrected.
    """
    if not (snapshot.protected_paths or snapshot.protected_deletions):
        return
    _require_objects(repo, snapshot)
    for rel in snapshot.protected_deletions:
        _safe_unlink(repo, rel)
    if not snapshot.protected_paths:
        return
    with _temp_workspace() as tmpdir:
        work_index = tmpdir / "index-protected"
        gitops.run_with_temp_index(
            repo, work_index, "read-tree", snapshot.worktree_tree
        )
        for rel in snapshot.protected_paths:
            _prepare_destination(repo, rel)
        gitops.run_with_temp_index(
            repo, work_index, "checkout-index", "-f", "-z", "--stdin",
            stdin="".join(f"{rel}\0" for rel in snapshot.protected_paths),
        )


def _require_objects(repo: Path, snapshot: GitRecoverySnapshot) -> None:
    if not gitops.object_exists(repo, snapshot.worktree_tree):
        raise SnapshotError(
            f"snapshot {snapshot.ref} worktree tree {snapshot.worktree_tree} "
            "is missing; cannot restore"
        )


def _restore_raw_index(repo: Path, snapshot: GitRecoverySnapshot) -> None:
    """Restore the real index bytes exactly (staging state + conflict stages).

    Written atomically beside the index (temp file + ``os.replace``) so a
    crash mid-restore never leaves a truncated index. Runs LAST: index stat
    data goes stale against the freshly materialized worktree, which only
    makes Git re-verify content — porcelain semantics are unchanged.
    """
    index_path = gitops.git_index_path(repo)
    if snapshot.raw_index_oid is None:
        if index_path.exists():
            index_path.unlink()
        return
    data = gitops.cat_file_blob(repo, snapshot.raw_index_oid)
    tmp = index_path.parent / f"{index_path.name}.gauntlet-restore-{os.getpid()}"
    tmp.write_bytes(data)
    os.replace(tmp, index_path)


def _safe_unlink(repo: Path, rel: str) -> None:
    """Remove one repo-relative file without ever following a symlink.

    Validates containment, refuses to traverse a symlinked parent component
    (deleting through it would reach outside the observed path), and prunes
    now-empty parent directories so a restored deletion leaves no husk.
    """
    _validate_rel_path(rel, field="restore path")
    parts = PurePosixPath(rel).parts
    current = repo
    for part in parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return  # nothing to remove
        if stat.S_ISLNK(info.st_mode):
            return  # never operate through a symlinked component
    target = repo / rel
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        raise SnapshotError(
            f"refusing to remove directory {rel!r} during restore: expected a "
            "file or symlink entry"
        )
    target.unlink()
    _prune_empty_dirs(repo, target.parent)


def _prune_empty_dirs(repo: Path, directory: Path) -> None:
    root = repo.resolve()
    current = directory
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved == root or root not in resolved.parents:
            return
        try:
            current.rmdir()
        except OSError:
            return  # not empty (or gone) — stop pruning
        current = current.parent


def _prepare_destination(repo: Path, rel: str) -> None:
    """Make ``repo/rel`` safe for Git to materialize into.

    Guarantees ``checkout-index`` can never write *through* a pre-existing
    symlink: any symlinked parent component is unlinked (a tree path can
    never legitimately live under a symlink — Git trees do not nest entries
    beneath symlinks), a regular file blocking a needed directory is removed,
    and a symlink or directory occupying the leaf is removed so the entry is
    recreated with its captured kind.
    """
    _validate_rel_path(rel, field="restore path")
    parts = PurePosixPath(rel).parts
    current = repo
    for part in parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break  # checkout-index creates the remaining directories
        if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
            current.unlink()
            break
    target = repo / rel
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        target.unlink()  # never let git (or anyone) write through the link
    elif stat.S_ISDIR(info.st_mode):
        shutil.rmtree(target)


__all__ = [
    "GitRecoverySnapshot",
    "RECOVERY_REF_PREFIX",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotError",
    "create_snapshot",
    "load_snapshot",
    "restore_protected",
    "restore_snapshot",
    "snapshot_ref",
]
