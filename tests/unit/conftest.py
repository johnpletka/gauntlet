"""Shared fixtures for P3 engine unit tests.

Git operations run against throwaway fixture repos under tmp dirs (plan P3 test
strategy). Adapters are faked and injected via the orchestrator's
``adapter_factory`` so the whole engine is exercised offline — no creds.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from gauntlet.adapters.base import AdapterCapabilities, AgentResult, Usage
from gauntlet.engine.recovery import (
    ABSENT_GIT_ENTRY,
    GitDelta,
    GitEntryKind,
    GitEntryObservation,
    GitEntryVersion,
    GitIndexStage,
    GitRenameObservation,
    RenamePlane,
    derive_git_delta,
    fingerprint_data,
)


def git(repo: Path, *args: str, message: str | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


@pytest.fixture(autouse=True)
def _no_caffeinate(monkeypatch):
    """Never spawn `caffeinate` from the unit suite.

    ``keep_awake`` defaults to true (#134), so every test that drives a run
    would otherwise fork a real `caffeinate -i -w <pid>` on darwin. The pure
    command builder (`keep_awake_command`) keeps its own tests; only the
    spawning context manager is neutralised here.
    """
    from gauntlet.engine import heartbeat

    monkeypatch.setattr(heartbeat.KeepAwake, "__enter__", lambda self: self)


@pytest.fixture(autouse=True)
def _isolated_usage_ledger(tmp_path, monkeypatch):
    """Redirect the machine-global usage ledger (FR-10) to a per-test temp path.

    The engine appends a content-free per-step usage row to
    ``~/.gauntlet/usage-ledger.jsonl`` on every step (P10). Autouse so no unit
    test — including the many that drive a full run — ever writes to the real
    machine-global ledger; each test gets an isolated, throwaway file.
    """
    from gauntlet.engine.ledger import LEDGER_PATH_ENV

    monkeypatch.setenv(LEDGER_PATH_ENV, str(tmp_path / "usage-ledger.jsonl"))


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@gauntlet.local")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("fixture\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "-M", "main")  # stable base branch name across git versions
    return repo


def run_work_tree(repo: Path, slug: str = "demo", *, prefix: str = "gauntlet/") -> Path:
    """The tree a run's agents edit and the engine commits in (P7g).

    P7g makes `dedicated` the default, so an assertion about a file an agent
    wrote, or about the branch a run has checked out, must name THIS tree — for
    a `dedicated` run it is not the operator's checkout, and asserting there is
    asserting the thing P7 exists to stop being true.

    Resolved from git's own ``worktree list`` rather than from
    ``gauntlet.engine.worktree``'s derivation, for the same reason
    :func:`_registered_run_worktrees` re-derives its root: these helpers are the
    check ON the engine, and sharing the engine's derivation would let one bug
    satisfy both sides. Falls back to ``repo`` when no worktree holds the run
    branch, which is exactly the `same_tree` answer.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return repo
    current: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{prefix}{slug}" and current is not None:
            return current
    return repo


def await_sentinel(repo: Path, rel: str, proc=None, *, timeout: float = 30.0) -> Path:
    """Block until a crash-child sentinel appears — in whichever tree it drives.

    The crash-injection harness signals "the agent step is now mid-flight" by
    dropping a file from inside the step: the agent adapter writes it into its
    ``cwd``, and a blocking shell step writes it into the tree it runs in. Both
    are the WORK tree, so under the P7g default the sentinel lands in the run
    worktree and never appears in the operator's checkout. Polling ``repo`` alone
    therefore timed out on every crash test — "child never reached the mid-step
    sentinel" — which is the harness encoding a `same_tree` truth, not the child
    failing to get there.

    Deliberately polls BOTH trees rather than being told which: the sentinel is
    plumbing, and a harness that had to know the layout would be one more thing
    to migrate the next time the layout moves. The partial edit the recovery
    property is actually about (``feature.py``) is untouched — it stays wherever
    the agent wrote it, which is the point of P7.

    Returns the sentinel's path. Raises if the child exits first or the deadline
    passes, with the same diagnosis the assertions used to carry.
    """
    import time

    candidates = [repo / rel] + sorted(
        (repo / ".gauntlet" / "worktrees").glob(f"*/*/{rel}")
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in candidates:
            if path.exists():
                return path
        # a tree created after we listed: re-glob each pass, it is cheap
        candidates = [repo / rel] + sorted(
            (repo / ".gauntlet" / "worktrees").glob(f"*/*/{rel}")
        )
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"child exited early ({proc.returncode})")
        time.sleep(0.01)
    raise AssertionError(
        f"child never reached the sentinel {rel!r} in {timeout}s "
        f"(looked in the operator's checkout and every run worktree under "
        f"{repo / '.gauntlet' / 'worktrees'})"
    )


def _git_bytes(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        check=True,
    )
    return proc.stdout


@dataclass
class RecoveryGitFixture:
    """Construct and inspect independent Git state planes in a throwaway repo.

    P1's recovery matrix needs more precision than the general ``fixture_repo``:
    HEAD, index, and worktree versions must remain separately observable, and
    symlinks must be inspected with ``lstat``/``readlink`` rather than followed.
    This helper deliberately uses Git plumbing for committed/index entries and
    filesystem primitives only for the worktree plane.
    """

    repo: Path

    def write(
        self, rel: str, content: str | bytes, *, executable: bool = False
    ) -> Path:
        path = self.repo / rel
        if path.is_symlink():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        path.write_bytes(data)
        path.chmod(0o755 if executable else 0o644)
        return path

    def symlink(self, rel: str, target: str | Path) -> Path:
        path = self.repo / rel
        if path.exists() or path.is_symlink():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        return path

    def delete(self, rel: str) -> None:
        path = self.repo / rel
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        elif path.exists() or path.is_symlink():
            path.unlink()

    def rename(self, source: str, destination: str) -> None:
        target = self.repo / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        (self.repo / source).rename(target)

    def stage(self, *paths: str) -> None:
        git(self.repo, "add", "-A", "--", *(paths or (".",)))

    def commit(self, message: str = "fixture state") -> str:
        git(self.repo, "commit", "-qm", message)
        return self.head_sha

    def switch(self, branch: str, *, create: bool = False) -> None:
        git(self.repo, "checkout", "-q" + ("b" if create else ""), branch)

    def create_branch(self, branch: str, start: str = "HEAD") -> None:
        git(self.repo, "branch", branch, start)

    def merge_conflict(self, branch: str) -> None:
        """Merge ``branch`` and require a genuine unresolved index conflict."""
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "merge", "--no-edit", branch],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            raise AssertionError("fixture merge unexpectedly completed without conflict")
        if not _git_bytes(self.repo, "ls-files", "--unmerged"):
            raise AssertionError(f"fixture merge failed without index stages: {proc.stderr}")

    @property
    def head_sha(self) -> str:
        return git(self.repo, "rev-parse", "HEAD").strip()

    @property
    def branch(self) -> str | None:
        branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        return None if branch == "HEAD" else branch

    def detach(self, ref: str = "HEAD") -> None:
        git(self.repo, "checkout", "-q", "--detach", ref)

    def status(self) -> str:
        return git(
            self.repo,
            "status",
            "--porcelain=v2",
            "--untracked-files=all",
        )

    def observe_path(self, rel: str, *, protected: bool = False) -> GitEntryObservation:
        head = self._tree_version("HEAD", rel)
        index, index_stages = self._index_version(rel)
        worktree = self._worktree_version(rel)
        if index_stages:
            index_delta = GitDelta.UNMERGED
            worktree_delta = GitDelta.CONFLICTED
        else:
            index_delta = derive_git_delta(head, index)
            worktree_delta = derive_git_delta(index, worktree, untracked=True)
        return GitEntryObservation(
            path=rel,
            head=head,
            index=index,
            worktree=worktree,
            index_delta=index_delta,
            worktree_delta=worktree_delta,
            index_stages=index_stages,
            protected=protected,
        )

    def observe_rename(
        self,
        source: str,
        destination: str,
        *,
        plane: RenamePlane,
        similarity: int = 100,
        protected: bool = False,
    ) -> GitEntryObservation:
        """Observe a rename with both source and destination plane evidence."""
        source_head = self._tree_version("HEAD", source)
        source_index, source_stages = self._index_version(source)
        source_worktree = self._worktree_version(source)
        if source_stages:
            raise AssertionError("rename source unexpectedly has unmerged index stages")

        head = self._tree_version("HEAD", destination)
        index, index_stages = self._index_version(destination)
        worktree = self._worktree_version(destination)
        if index_stages:
            raise AssertionError("rename destination unexpectedly has unmerged index stages")

        if plane is RenamePlane.INDEX:
            source_before, source_after = source_head, source_index
            index_delta = GitDelta.RENAMED
            worktree_delta = derive_git_delta(index, worktree, untracked=True)
        else:
            source_before, source_after = source_index, source_worktree
            index_delta = derive_git_delta(head, index)
            worktree_delta = GitDelta.RENAMED

        return GitEntryObservation(
            path=destination,
            head=head,
            index=index,
            worktree=worktree,
            index_delta=index_delta,
            worktree_delta=worktree_delta,
            rename=GitRenameObservation(
                source_path=source,
                plane=plane,
                source_before=source_before,
                source_after=source_after,
                similarity=similarity,
            ),
            protected=protected,
        )

    def _tree_version(self, ref: str, rel: str) -> GitEntryVersion:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "ls-tree", "-z", ref, "--", rel],
            capture_output=True,
            check=True,
        )
        if not proc.stdout:
            return ABSENT_GIT_ENTRY
        metadata, _path = proc.stdout[:-1].split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        return self._object_version(mode, kind, oid)

    def _index_version(
        self, rel: str
    ) -> tuple[GitEntryVersion, tuple[GitIndexStage, ...]]:
        output = _git_bytes(self.repo, "ls-files", "--stage", "-z", "--", rel)
        normal = ABSENT_GIT_ENTRY
        stages: list[GitIndexStage] = []
        for record in output.rstrip(b"\0").split(b"\0") if output else ():
            metadata, _path = record.split(b"\t", 1)
            mode, oid, stage_text = metadata.decode().split()
            version = self._object_version(mode, "blob", oid)
            stage = int(stage_text)
            if stage == 0:
                normal = version
            else:
                stages.append(GitIndexStage(stage=stage, version=version))
        return normal, tuple(stages)

    def _object_version(self, mode: str, kind: str, oid: str) -> GitEntryVersion:
        if mode == "120000":
            target = _git_bytes(self.repo, "cat-file", "blob", oid).decode(
                "utf-8", errors="surrogateescape"
            )
            return GitEntryVersion(
                kind=GitEntryKind.SYMLINK,
                mode=mode,
                object_id=oid,
                symlink_target=target,
            )
        entry_kind = (
            GitEntryKind.GITLINK if mode == "160000" or kind == "commit"
            else GitEntryKind.REGULAR
        )
        return GitEntryVersion(kind=entry_kind, mode=mode, object_id=oid)

    def _worktree_version(self, rel: str) -> GitEntryVersion:
        path = self.repo / rel
        try:
            info = path.lstat()
        except FileNotFoundError:
            return ABSENT_GIT_ENTRY
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            return GitEntryVersion(
                kind=GitEntryKind.SYMLINK,
                mode="120000",
                object_id=self._hash_object(target.encode()),
                symlink_target=target,
            )
        if stat.S_ISDIR(info.st_mode):
            entries = []
            for child in sorted(path.rglob("*")):
                child_info = child.lstat()
                rel_child = child.relative_to(path).as_posix()
                if stat.S_ISLNK(child_info.st_mode):
                    entries.append((rel_child, "symlink", os.readlink(child)))
                elif stat.S_ISREG(child_info.st_mode):
                    entries.append(
                        (rel_child, "file", self._hash_object(child.read_bytes()))
                    )
                elif stat.S_ISDIR(child_info.st_mode):
                    entries.append((rel_child, "directory", ""))
            return GitEntryVersion(
                kind=GitEntryKind.DIRECTORY,
                mode="040000",
                object_id=fingerprint_data(entries),
            )
        if stat.S_ISREG(info.st_mode):
            data = path.read_bytes()
            mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
            return GitEntryVersion(
                kind=GitEntryKind.REGULAR,
                mode=mode,
                object_id=self._hash_object(data),
            )
        return GitEntryVersion(
            kind=GitEntryKind.UNKNOWN,
            unknown_reason=f"unsupported filesystem mode {info.st_mode:o}",
        )

    def _hash_object(self, data: bytes) -> str:
        return _git_bytes(self.repo, "hash-object", "--stdin", stdin=data).decode().strip()

@pytest.fixture
def recovery_git(fixture_repo: Path) -> RecoveryGitFixture:
    return RecoveryGitFixture(fixture_repo)


class FakeAdapter:
    """A scriptable adapter: optionally writes files, returns text/usage."""

    name = "fake"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def __init__(
        self,
        *,
        text: str = "ok",
        writes: dict[str, str] | None = None,
        usage: Usage | None = None,
        session_id: str | None = "fake-session",
        structured=None,
        on_run=None,
    ) -> None:
        self.text = text
        self.writes = writes or {}
        self.usage = usage
        self.session_id = session_id
        self.structured = structured
        self.on_run = on_run
        self.calls: list[dict] = []
        self.timeout_s = 600.0

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.calls.append({"prompt": prompt, "session": session, "cwd": cwd})
        if self.on_run is not None:
            self.on_run(self, prompt, cwd)
        for rel, content in self.writes.items():
            target = Path(cwd) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return AgentResult(
            text=self.text,
            structured=self.structured,
            session_id=self.session_id,
            usage=self.usage,
            exit_code=0,
        )


@pytest.fixture
def fake_adapter_factory():
    """Return a factory that maps agent names to provided FakeAdapter instances."""

    def make(mapping: dict[str, FakeAdapter]):
        def factory(agent_name: str) -> FakeAdapter:
            return mapping[agent_name]

        return factory

    return make


# --- acceptance A1 as a property, not a case (P7c, spike §12.1) ---------------


def _operator_fingerprint(repo: Path) -> tuple[str, str, str, str]:
    """The four Git planes the plan §3 keeps independently observable.

    Branch and HEAD (per-worktree), the index, and the working tree. A1 is
    "starting, resuming, recovering and rolling back a run never changes the
    operator's checked-out branch, index, or worktree", so all four must be
    byte-identical across a verb — a partial fix that repoints only the commit
    path would still move the index or leave dirt, and this catches that.
    """
    import hashlib
    import subprocess

    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
        return proc.stdout if proc.returncode == 0 else f"<err:{proc.returncode}>"

    index = hashlib.sha256(run("ls-files", "-s", "--", ":/").encode()).hexdigest()
    # A1 protects the operator's EXISTING state: their branch, HEAD, index, and
    # the content of files they own. It is not a claim that a run produces
    # nothing for them. `PR.md` is the FR-9.8 final-gate deliverable, drafted
    # into the operator's slug dir precisely BECAUSE it is addressed to the
    # human — it is theirs to edit, commit and push (PRD §2.2), and a copy in
    # the run worktree would be destroyed by the `finish`/`clean` teardown that
    # immediately follows. Counting the arrival of that deliverable as a
    # violation would force it somewhere it cannot serve its purpose.
    #
    # Scoped narrowly on purpose: ONLY this engine-authored, human-owned
    # deliverable is filtered, and only in the worktree plane. Branch, HEAD and
    # index stay byte-exact, and any change the run makes to a file that
    # already existed still fails.
    worktree_plane = "\n".join(
        line for line in run("status", "--porcelain", "--untracked-files=all").splitlines()
        if not line.endswith("/PR.md")
    )
    return (
        run("rev-parse", "--abbrev-ref", "HEAD").strip(),
        run("rev-parse", "HEAD").strip(),
        index,
        worktree_plane,
    )


def _registered_run_worktrees(repo: Path) -> set[str]:
    """Resolved paths of worktrees under the engine's own worktrees root.

    The evidence that a test actually exercised the `dedicated` layout. Scoped
    to the engine's derived root so an adopter's (or a test's) own linked
    worktree is never mistaken for a run worktree.
    """
    import subprocess

    from gauntlet.engine import worktree as WT

    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return set()
    paths = [
        Path(line[len("worktree "):])
        for line in proc.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    if not paths:
        return set()
    # P7e: the derived root hangs off the MAIN worktree, which git reports
    # first, rather than off the git common dir. Deliberately re-derived from
    # git here rather than imported from the engine — this fixture is the check
    # ON the engine, so sharing the engine's own derivation would let one bug
    # satisfy both sides.
    main_root = paths[0].resolve()
    return {
        str(p.resolve())
        for p in paths
        if WT.is_inside_worktrees_root(p, main_root)
    }


def _a1_drift(repo: Path, before: tuple[str, str, str, str]) -> str | None:
    """The A1 violation message for one bracket, or ``None`` when it held."""
    after = _operator_fingerprint(repo)
    planes = ("branch", "HEAD", "index", "worktree")
    drifted = [p for p, b, a in zip(planes, before, after) if b != a]
    if not drifted:
        return None
    return (
        "P7 acceptance A1 violated: this test created a dedicated run worktree, "
        f"but the OPERATOR's checkout changed on: {', '.join(drifted)}.\n"
        f"  before: {before}\n  after:  {after}\n"
        "A run with its own worktree must never touch the operator's branch, "
        "HEAD, index or working tree (spike §9/§12.1). Route the offending "
        "call through `work_root` rather than the operator's checkout."
    )


@pytest.fixture(autouse=True)
def operator_checkout_invariance(request):
    """Assert acceptance A1 on EVERY test that drives a dedicated run.

    Spike §12.1 asks for A1 as a *property* over the existing verb suite rather
    than a handful of cases, because a property fails the moment any one of the
    25 root-conflation sites in §9 is missed, and a case only fails if someone
    thought to write it. P7g makes `dedicated` the default, so this now fires
    across the whole suite rather than over the handful of tests that opted in.

    **Why it is conditional, and why that is not a weakening.** A `same_tree`
    run drives the operator's checkout *by definition* — that is the pre-P7
    layout, it remains the mode of every legacy run and the documented fallback
    (§16), and asserting invariance there would assert that the engine does
    nothing. So the fixture keys on EVIDENCE rather than on configuration: it
    asserts invariance exactly when the test actually created a run worktree
    under the engine's derived root. That also stops a `--same-tree` fallback
    test from claiming coverage it does not have.

    **Why the bracket is per-DRIVE (P7g, design problem D).** Through P7f the
    baseline was captured once, at the first tree creation, and compared once at
    teardown. That single bracket spans everything a test does after its first
    run — including a legitimate commit in the operator's checkout BETWEEN two
    runs, which is a shape the flip makes common
    (`test_lock_released_on_done_and_park` is exactly it, and P7d spot-checked
    it as a false positive). A strict-but-imprecise property gets suppressed the
    first time it cries wolf, which is the failure this docstring's own earlier
    warning named.

    So the bracket is now :meth:`RunManager._run_paths` — the region in which a
    run drives with resolved roots, entered by exactly the verbs A1 names
    (`start`, `resume`, `approve`/`reject`, `rollback`) and nesting correctly
    for an auto-resume inside a drive. Each drive is fingerprinted and checked
    on its own, so N drives are N independent assertions instead of one
    aggregate, and what a test does between drives is its own business. This is
    strictly MORE checking, not less: the only interval dropped is the one
    BETWEEN verbs, which A1 makes no claim about.

    `migrate-worktree` creates a tree outside any `_run_paths` bracket (it is
    the one other `WT.ensure` caller in the engine), so it keeps the pre-P7g
    capture-once/compare-at-teardown treatment rather than silently losing its
    coverage.
    """
    if "fixture_repo" not in request.fixturenames:
        yield
        return
    # A1 is scoped to the four verbs it names: "starting, resuming, recovering
    # and rolling back a run never changes the operator's checked-out branch,
    # index, or worktree." `finish` and `clean` are deliberately NOT in that
    # list — merging the run branch into the base and deleting it FROM the
    # operator's checkout is their entire purpose (spike §9.4, and the reason
    # `RunManager.operator_root` exists as a separate name). A test that drives
    # them marks itself, so the exemption is explicit and greppable rather than
    # inferred from which verb happened to run.
    if request.node.get_closest_marker("operator_tree_verb") is not None:
        yield
        return
    repo = request.getfixturevalue("fixture_repo")

    import contextlib

    from gauntlet.engine import worktree as WT
    from gauntlet.engine.run import RunManager

    # Violations are COLLECTED and asserted at teardown rather than raised
    # inside the drive: several engine drive paths carry broad `except`
    # handlers, and an assertion raised into one of them would be swallowed and
    # re-reported as a park reason instead of as a test failure.
    violations: list[str] = []
    depth = 0            # `_run_paths` nesting; only the outermost is bracketed
    exercised = False    # was a dedicated tree created inside this bracket?
    outside: dict = {}   # the `migrate-worktree` fallback bracket

    real_ensure, real_recreate = WT.ensure, WT.recreate
    real_run_paths = RunManager._run_paths

    def _mark():
        nonlocal exercised
        if depth:
            exercised = True
        elif not outside:
            # A tree created outside any drive — `migrate-worktree`, the one
            # other `WT.ensure` caller. Keep the pre-P7g bracket: baseline
            # here, compared once at teardown.
            outside["fp"] = _operator_fingerprint(repo)

    def ensure(*a, **kw):
        _mark()
        return real_ensure(*a, **kw)

    def recreate(*a, **kw):
        _mark()
        return real_recreate(*a, **kw)

    @contextlib.contextmanager
    def _bracket(cm):
        nonlocal depth, exercised
        outermost = depth == 0
        before = _operator_fingerprint(repo) if outermost else None
        if outermost:
            exercised = False
        depth += 1
        try:
            with cm as value:
                yield value
        finally:
            depth -= 1
            # F-009's rule, unchanged: keyed on whether a tree was EXERCISED,
            # not on whether one is still registered afterwards. A drive that
            # removes its own tree (a `--same-tree` fallback) is exactly the
            # population most likely to touch the operator's checkout.
            if outermost and exercised:
                drift = _a1_drift(repo, before)
                if drift is not None:
                    violations.append(drift)

    def run_paths(self, *a, **kw):
        return _bracket(real_run_paths(self, *a, **kw))

    WT.ensure, WT.recreate = ensure, recreate
    RunManager._run_paths = run_paths
    try:
        yield
    finally:
        WT.ensure, WT.recreate = real_ensure, real_recreate
        RunManager._run_paths = real_run_paths
    if outside:
        drift = _a1_drift(repo, outside["fp"])
        if drift is not None:
            violations.append(drift)
    assert not violations, "\n\n".join(violations)
