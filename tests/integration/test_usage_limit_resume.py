"""The decisive P1 test (harness-efficiency §1.3, FR-3.3): a real claude-code
session interrupted mid-task and resumed continues the SAME session, with the
file state produced before the interruption intact.

This is the core-bet check: if an interrupted CLI session can be continued
(same session id, preserved worktree, short continuation prompt) and produce
work equivalent to an uninterrupted run, a usage-limit halt becomes a pause,
not a restart. Marked `integration` (needs live claude creds); CI runs
`pytest -m "not integration"`, and the builder runs this locally before handoff.
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


def _write_adapter():
    # Write mode in a disposable fixture repo only (pins.yaml: acceptEdits +
    # allowedTools Write). No permission-bypass flags (rejected by the lint).
    return ClaudeCodeAdapter(
        model="haiku",
        permission_mode="acceptEdits",
        allowed_tools=["Write"],
        timeout_s=TIMEOUT_S,
    )


def test_interrupted_session_resumes_with_worktree_intact(fixture_repo):
    adapter = _write_adapter()
    # First turn: do part of the work — write step1.txt — then stop (as if the
    # usage limit hit right after this milestone).
    first = adapter.run(
        "Create a file named step1.txt in the current directory containing "
        "exactly the text ZIRCON-42 (no trailing newline needed). Then reply OK.",
        cwd=fixture_repo,
    )
    assert first.session_id, "a resumable session id must be captured"
    step1 = fixture_repo / "step1.txt"
    assert step1.exists(), "the pre-interruption milestone file must exist"
    assert "ZIRCON-42" in step1.read_text()

    # Resume the SAME session with a short continuation prompt (FR-3.3 shape).
    # The worktree is left exactly as the first turn produced it.
    second = adapter.run(
        "You were interrupted by a provider usage limit before finishing. "
        "Continue: create step2.txt in the current directory containing the "
        "same codeword you wrote into step1.txt earlier. Then reply DONE.",
        session=first.session_id,
        cwd=fixture_repo,
    )
    assert second.exit_code == 0

    # The pre-interruption file survived untouched (worktree preserved)...
    assert step1.exists() and "ZIRCON-42" in step1.read_text()
    # ...and the resumed session carried the earlier context to finish the work,
    # writing the codeword it only knew from the first (interrupted) turn.
    step2 = fixture_repo / "step2.txt"
    assert step2.exists(), "the resumed session must complete the remaining work"
    assert "ZIRCON-42" in step2.read_text()
