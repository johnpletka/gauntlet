"""Shared fixtures for the contract suite.

Plan P1 test constraints (review F-002 — the judge does not exist yet, so
these are the compensating control):
- smoke prompts are tool-less text round-trips;
- codex runs `--sandbox read-only` and claude runs with no tools allowed,
  except where a write-mode flag is itself under test;
- write-mode tests run only in disposable fixture repos under a temp dir,
  never against this repo.
"""

import os
import signal
import subprocess

import pytest

from gauntlet.engine.judgeproc import _MANAGED_ENV_VARS


def _judge_serve_pids() -> set[int]:
    """PIDs of every live `gauntlet judge serve` process on this machine."""
    proc = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True
    )
    pids: set[int] = set()
    for line in proc.stdout.splitlines():
        pid_str, _, cmd = line.strip().partition(" ")
        if "gauntlet judge serve" in cmd:
            pids.add(int(pid_str))
    return pids


@pytest.fixture(scope="session", autouse=True)
def _no_leaked_judge_servers():
    """Fail the session if it leaks a `gauntlet judge serve` process (#85).

    The uv-run wrapper bug orphaned two servers per suite run for months and
    was invisible to the suite itself — the wrapper exits cleanly, so nothing
    reported the leak. Diff live server PIDs across the session so a
    regression cannot hide again; kill any survivor so a failure here does
    not itself accumulate orphans.
    """
    before = _judge_serve_pids()
    yield
    leaked = _judge_serve_pids() - before
    for pid in leaked:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    assert not leaked, (
        f"integration session leaked `gauntlet judge serve` PIDs {sorted(leaked)} "
        "(killed); a fixture is terminating a wrapper instead of the process group"
    )


@pytest.fixture(autouse=True)
def _isolated_usage_ledger(tmp_path, monkeypatch):
    """Redirect the machine-global usage ledger (FR-10) to a temp path.

    An integration test that drives a real run would otherwise append rows to
    the operator's real ``~/.gauntlet/usage-ledger.jsonl``; point it at a
    throwaway file per test so the contract suite leaves no machine-global trace.
    """
    from gauntlet.engine.ledger import LEDGER_PATH_ENV

    monkeypatch.setenv(LEDGER_PATH_ENV, str(tmp_path / "usage-ledger.jsonl"))


@pytest.fixture(autouse=True)
def _sanitize_gauntlet_env():
    """Clear engine-managed GAUNTLET_* vars for each integration test, restoring
    them after.

    The suite assumes a clean process environment: several tests assert
    `GAUNTLET_JUDGE_TOKEN not in os.environ` as a precondition, and the
    engine/hook key judge gating on GAUNTLET_RUN_ID. An operator who exports
    GAUNTLET_JUDGE_TOKEN (or other GAUNTLET_* vars) globally — e.g. in
    ~/.zshenv — would otherwise leak them into os.environ and trip those
    preconditions or alter behavior. Tests that need these vars set them
    explicitly on the subprocess env they build, so clearing the inherited
    values here is safe.
    """
    saved = {v: os.environ.pop(v, None) for v in _MANAGED_ENV_VARS}
    try:
        yield
    finally:
        for v, val in saved.items():
            if val is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = val


@pytest.fixture
def fixture_repo(tmp_path):
    """Disposable git repo under a temp dir for write-mode flag tests."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("disposable fixture repo for gauntlet P1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gauntlet Test",
            "-c",
            "user.email=test@gauntlet.local",
            "commit",
            "-qm",
            "init",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def run_work_tree(repo, slug: str = "demo", *, prefix: str = "gauntlet/"):
    """The tree a run's agents edit and the engine commits in (P7g).

    The deliberate twin of ``tests/unit/conftest.py``'s helper of the same name.
    Duplicated rather than shared because a ``conftest.py`` is not importable
    from a sibling test directory, and routing this through a third module would
    put the check ON the engine further from the tests that make it.

    P7g makes `dedicated` the default, so an assertion about a file an agent
    wrote, or about the branch a run has checked out, must name THIS tree — for
    a `dedicated` run it is not the operator's checkout, and asserting there is
    asserting the very thing P7 exists to stop being true.

    Resolved from git's own ``worktree list`` rather than from
    ``gauntlet.engine.worktree``'s derivation: these helpers are the check on
    the engine, and sharing its derivation would let one bug satisfy both sides.
    Falls back to ``repo`` when no worktree holds the run branch, which is
    exactly the `same_tree` answer.
    """
    from pathlib import Path

    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return repo
    current = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{prefix}{slug}" and current is not None:
            return current
    return repo
