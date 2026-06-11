"""Permission-bypass / hook-disabling flag lint (PRD §8)."""

import pytest

from gauntlet.adapters.claude_code import ClaudeCodeAdapter
from gauntlet.adapters.codex import CodexAdapter
from gauntlet.config import BannedFlagError, lint_flags


@pytest.mark.parametrize(
    "argv",
    [
        ["claude", "-p", "--dangerously-skip-permissions"],
        ["claude", "-p", "--allow-dangerously-skip-permissions"],
        ["claude", "-p", "--bare"],
        ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
        ["codex", "exec", "--yolo"],
        ["codex", "exec", "--dangerously-bypass-hook-trust"],
        ["claude", "--permission-mode", "bypassPermissions"],
        ["claude", "--permission-mode=bypassPermissions"],
        ["codex", "exec", "--sandbox", "danger-full-access"],
        ["codex", "exec", "-s", "danger-full-access"],
        ["codex", "exec", "-s=danger-full-access"],
    ],
)
def test_banned_argv_rejected(argv):
    with pytest.raises(BannedFlagError):
        lint_flags(argv)


def test_benign_argv_passes():
    lint_flags(
        [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Read,Grep",
        ]
    )
    lint_flags(["codex", "exec", "--json", "--sandbox", "read-only", "-"])


def test_claude_adapter_rejects_banned_base_flags():
    with pytest.raises(BannedFlagError):
        ClaudeCodeAdapter(base_flags=["--dangerously-skip-permissions"])


def test_claude_adapter_rejects_bypass_permission_mode():
    with pytest.raises(BannedFlagError):
        ClaudeCodeAdapter(permission_mode="bypassPermissions")


def test_claude_adapter_rejects_banned_extra_flags():
    adapter = ClaudeCodeAdapter()
    with pytest.raises(BannedFlagError):
        adapter.run("hi", extra_flags=["--dangerously-skip-permissions"])


def test_codex_adapter_rejects_danger_sandbox():
    with pytest.raises(BannedFlagError):
        CodexAdapter(sandbox="danger-full-access")


def test_codex_adapter_rejects_banned_extra_flags(tmp_path):
    adapter = CodexAdapter()
    with pytest.raises(BannedFlagError):
        adapter.run(
            "hi",
            cwd=tmp_path,
            extra_flags=["--dangerously-bypass-approvals-and-sandbox"],
        )
