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
