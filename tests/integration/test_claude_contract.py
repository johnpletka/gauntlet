"""Contract tests against the installed `claude` CLI (plan P1).

Verified behavior lands in the doctor pin file. Tool-less prompts; the one
write-mode test runs in a disposable fixture repo only.
"""

import shutil

import pytest

from gauntlet.adapters.claude_code import ClaudeCodeAdapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("claude") is None, reason="claude CLI not installed"
    ),
]

TIMEOUT_S = 300.0


def toolless_adapter(**kwargs):
    # no tools available at all; cheap+fast model for smoke traffic
    defaults = dict(model="haiku", tools=[], timeout_s=TIMEOUT_S)
    defaults.update(kwargs)
    return ClaudeCodeAdapter(**defaults)


def test_smoke_toolless_json(tmp_path):
    result = toolless_adapter().run(
        "Reply with exactly: GAUNTLET_PONG", cwd=tmp_path
    )
    assert "GAUNTLET_PONG" in result.text
    assert result.exit_code == 0
    assert result.session_id  # resumable session captured
    assert result.usage is not None
    assert (result.usage.output_tokens or 0) > 0
    assert result.raw_events  # lossless event capture


def test_resume_continuity(tmp_path):
    adapter = toolless_adapter()
    first = adapter.run(
        "The codeword is ZIRCON-42. Reply with exactly: OK", cwd=tmp_path
    )
    assert first.session_id
    second = adapter.run(
        "Reply with the codeword I gave you earlier, and nothing else.",
        session=first.session_id,
        cwd=tmp_path,
    )
    assert "ZIRCON-42" in second.text


def test_structured_output_json_schema(tmp_path):
    schema = {
        "type": "object",
        "properties": {"capital": {"type": "string"}},
        "required": ["capital"],
        "additionalProperties": False,
    }
    result = toolless_adapter().run(
        "What is the capital of France?", schema=schema, cwd=tmp_path
    )
    assert result.structured is not None
    assert result.structured["capital"].lower().startswith("paris")


def test_stream_json_capture(tmp_path):
    result = toolless_adapter(output_format="stream-json").run(
        "Reply with exactly: STREAMED", cwd=tmp_path
    )
    assert "STREAMED" in result.text
    types = {e.get("type") for e in result.raw_events}
    assert "result" in types
    assert len(result.raw_events) >= 2  # init/assistant events plus result


def test_effort_flag(tmp_path):
    # --effort is a new surface added by this adapter; verify the installed
    # CLI accepts it without error before treating it as pinned behavior.
    result = toolless_adapter(effort="low").run(
        "Reply with exactly: GAUNTLET_PONG", cwd=tmp_path
    )
    assert result.exit_code == 0
    assert "GAUNTLET_PONG" in result.text


def test_help_surface_effort_flag():
    import subprocess

    help_out = subprocess.run(
        ["claude", "--help"], capture_output=True, text=True, timeout=30
    ).stdout
    assert "--effort" in help_out, "--effort absent from claude --help"


def test_write_flag_in_disposable_fixture_repo(fixture_repo):
    # The write-mode flag is itself under test here (plan F-002 carve-out):
    # acceptEdits + Write tool, in a throwaway repo under tmp, never this repo.
    adapter = ClaudeCodeAdapter(
        model="haiku",
        permission_mode="acceptEdits",
        tools=["Write"],
        allowed_tools=["Write"],
        timeout_s=TIMEOUT_S,
    )
    result = adapter.run(
        "Create a file named hello.txt in the current directory containing "
        "exactly: hi",
        cwd=fixture_repo,
    )
    assert result.exit_code == 0
    target = fixture_repo / "hello.txt"
    assert target.exists(), f"agent did not create the file; said: {result.text!r}"
    assert "hi" in target.read_text()
