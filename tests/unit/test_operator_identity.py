"""Operator identity resolution (FR-9, plan P2).

Pins the deterministic, fail-closed precedence for the audit ``user`` field:
- ``GAUNTLET_USER_EMAIL`` wins only when non-empty after trimming;
- otherwise ``git config user.email``, trimmed;
- neither resolvable → raise the exact FR-9 message and record nothing;
- a non-zero / missing ``git config`` is a failure, never a silent empty value;
- ``config.identity()`` (``@gauntlet.local``) is never consulted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gauntlet.engine.identity import (
    GAUNTLET_USER_EMAIL,
    OperatorIdentityError,
    resolve_operator_identity,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo_with_email(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "git-config@example.com")
    return repo


@pytest.fixture
def repo_without_email(tmp_path: Path, monkeypatch) -> Path:
    """A git repo whose ``user.email`` is unset → ``git config`` exits non-zero.

    Points ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` at a nonexistent path so
    the lookup fails closed regardless of the host developer's real global git
    config (e.g. a machine-wide ``user.email``).
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system-gitconfig"))
    repo = tmp_path / "repo-noemail"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_env_wins_and_is_trimmed(monkeypatch, repo_with_email: Path) -> None:
    """Whitespace-padded env var wins over git config and is returned trimmed."""
    monkeypatch.setenv(GAUNTLET_USER_EMAIL, "  operator@example.com  ")
    assert resolve_operator_identity(repo_with_email) == "operator@example.com"


def test_empty_env_falls_back_to_git_config(monkeypatch, repo_with_email: Path) -> None:
    """An exported-but-empty env var does not shadow a valid git config."""
    monkeypatch.setenv(GAUNTLET_USER_EMAIL, "   ")
    assert resolve_operator_identity(repo_with_email) == "git-config@example.com"


def test_unset_env_uses_git_config(monkeypatch, repo_with_email: Path) -> None:
    monkeypatch.delenv(GAUNTLET_USER_EMAIL, raising=False)
    assert resolve_operator_identity(repo_with_email) == "git-config@example.com"


def test_both_unset_fails_closed_with_exact_message(
    monkeypatch, repo_without_email: Path
) -> None:
    """env blank + git config non-zero → raise the verbatim FR-9 message."""
    monkeypatch.delenv(GAUNTLET_USER_EMAIL, raising=False)
    with pytest.raises(OperatorIdentityError) as exc:
        resolve_operator_identity(repo_without_email)
    assert str(exc.value) == (
        "cannot resolve operator identity for the audit trail: set "
        "`GAUNTLET_USER_EMAIL` or `git config user.email`"
    )


def test_git_config_nonzero_with_blank_env_raises_not_silent_empty(
    monkeypatch, repo_without_email: Path
) -> None:
    """A failed git config lookup raises rather than recording an empty value."""
    monkeypatch.setenv(GAUNTLET_USER_EMAIL, "\t  \n")
    with pytest.raises(OperatorIdentityError):
        resolve_operator_identity(repo_without_email)


def test_missing_git_binary_fails_closed(monkeypatch, tmp_path: Path) -> None:
    """OSError from a missing git binary is fail-closed, not a silent empty."""
    monkeypatch.delenv(GAUNTLET_USER_EMAIL, raising=False)

    def _boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(OperatorIdentityError):
        resolve_operator_identity(tmp_path)


def test_malformed_but_nonblank_value_recorded_verbatim(monkeypatch, tmp_path: Path) -> None:
    """v1 enforces non-empty only — a non-RFC-5322 value is returned verbatim."""
    monkeypatch.setenv(GAUNTLET_USER_EMAIL, "  not-an-email  ")
    assert resolve_operator_identity(tmp_path) == "not-an-email"


def test_env_injection_overrides_os_environ(repo_with_email: Path) -> None:
    """The optional env mapping is honoured (deterministic, no process state)."""
    assert (
        resolve_operator_identity(repo_with_email, env={GAUNTLET_USER_EMAIL: "x@y.z"})
        == "x@y.z"
    )
    assert (
        resolve_operator_identity(repo_with_email, env={}) == "git-config@example.com"
    )
