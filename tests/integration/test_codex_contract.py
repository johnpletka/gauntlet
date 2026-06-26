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


# F-005 (P1 review round 1): pin-file claims need matching contract tests.
def test_resume_with_output_schema(fixture_repo):
    # P4's diff-scoped confirm pass leans on exactly this combination:
    # exec resume + --output-schema + -o.
    adapter = CodexAdapter(sandbox="read-only", timeout_s=TIMEOUT_S)
    first = adapter.run(
        "The codeword is OBSIDIAN-7. Reply with exactly: OK", cwd=fixture_repo
    )
    assert first.session_id
    schema = {
        "type": "object",
        "properties": {"codeword": {"type": "string"}},
        "required": ["codeword"],
        "additionalProperties": False,
    }
    second = adapter.run(
        "Report the codeword I gave you earlier via the schema.",
        session=first.session_id,
        schema=schema,
        cwd=fixture_repo,
    )
    assert second.structured["codeword"] == "OBSIDIAN-7"
    assert second.usage is not None


def test_help_surface_matches_pin_file():
    # Backs the pin-file divergence claims with assertions against the
    # installed binary, not memory of its docs.
    import subprocess

    exec_help = subprocess.run(
        ["codex", "exec", "--help"], capture_output=True, text=True, timeout=30
    ).stdout
    resume_help = subprocess.run(
        ["codex", "exec", "resume", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert "--full-auto" not in exec_help  # PRD §4.1 mentions it; 0.139.0 lacks it
    for flag in ("--json", "--output-schema", "--output-last-message"):
        assert flag in exec_help, flag
        assert flag in resume_help, flag
    assert "--sandbox" in exec_help
    assert "--sandbox" not in resume_help  # resume re-pins via -c sandbox_mode


def test_reasoning_effort_config_key(fixture_repo):
    # model_reasoning_effort is a -c config key, not a named CLI flag; it will
    # not appear in `codex exec --help`. Verify the installed CLI accepts it
    # without error before treating it as pinned behavior.
    adapter = CodexAdapter(
        sandbox="read-only", reasoning_effort="low", timeout_s=TIMEOUT_S
    )
    result = adapter.run(
        "Reply with exactly: GAUNTLET_PONG", cwd=fixture_repo
    )
    assert result.exit_code == 0
    assert "GAUNTLET_PONG" in result.text


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
