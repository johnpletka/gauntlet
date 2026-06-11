"""Shared fixtures for P3 engine unit tests.

Git operations run against throwaway fixture repos under tmp dirs (plan P3 test
strategy). Adapters are faked and injected via the orchestrator's
``adapter_factory`` so the whole engine is exercised offline — no creds.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gauntlet.adapters.base import AdapterCapabilities, AgentResult, Usage


def git(repo: Path, *args: str, message: str | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


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
