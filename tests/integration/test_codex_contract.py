"""Contract tests against the installed `codex` CLI (plan P1).

Read-only sandbox throughout, except the workspace-write test, which runs in
a disposable fixture repo only.
"""

import shutil

import pytest

from gauntlet.adapters.codex import CodexAdapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("codex") is None, reason="codex CLI not installed"
    ),
]

TIMEOUT_S = 300.0

SCHEMA = {
    "type": "object",
    "properties": {"capital": {"type": "string"}},
    "required": ["capital"],
    "additionalProperties": False,
}


def test_smoke_readonly_with_output_schema(fixture_repo):
    adapter = CodexAdapter(sandbox="read-only", timeout_s=TIMEOUT_S)
    result = adapter.run(
        "What is the capital of France? Answer via the schema.",
        schema=SCHEMA,
        cwd=fixture_repo,
    )
    assert result.exit_code == 0
    assert result.structured["capital"].lower().startswith("paris")
    assert result.session_id  # thread id captured for resume
    assert result.usage is not None
    assert (result.usage.input_tokens or 0) > 0
    assert (result.usage.output_tokens or 0) > 0
    assert result.usage.cost_usd is None  # tokens-only degraded path
    assert result.raw_events


def test_resume_continuity(fixture_repo):
    adapter = CodexAdapter(sandbox="read-only", timeout_s=TIMEOUT_S)
    first = adapter.run(
        "The codeword is ZIRCON-42. Reply with exactly: OK", cwd=fixture_repo
    )
    assert first.session_id
    second = adapter.run(
        "Reply with the codeword I gave you earlier, and nothing else.",
        session=first.session_id,
        cwd=fixture_repo,
    )
    assert "ZIRCON-42" in second.text


def test_workspace_write_in_disposable_fixture_repo(fixture_repo):
    # The write-mode sandbox flag is itself under test (plan F-002 carve-out).
    adapter = CodexAdapter(sandbox="workspace-write", timeout_s=TIMEOUT_S)
    result = adapter.run(
        "Create a file named hello.txt in the current directory containing "
        "exactly: hi",
        cwd=fixture_repo,
    )
    assert result.exit_code == 0
    target = fixture_repo / "hello.txt"
    assert target.exists(), f"agent did not create the file; said: {result.text!r}"
    assert "hi" in target.read_text()
