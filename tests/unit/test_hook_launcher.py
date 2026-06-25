"""The wired PreToolUse command is an install-tolerant launcher (fix/judge-hook).

These tests execute the *exact* ``HOOK_WIRED_COMMAND`` string under ``/bin/sh`` —
the shell Claude Code / Codex use for ``command``-type hooks — to prove its four
branches without needing a live CLI:

* hook installed, allow → stdin + stdout + exit 0 pass straight through;
* hook installed, DENY → the exit-2 decision is **not** masked by the ``|| …`` tail
  (``exec`` replaces the process, so it cannot be) — the load-bearing safety property;
* hook missing, plain session → silent exit 0, no output (the zero-notice goal that
  motivates this wiring: a teammate who never installed Gauntlet sees nothing);
* hook missing, inside an active run (GAUNTLET_RUN_ID set) → fail closed (exit 2),
  so a broken install can never let a run proceed silently ungated (CLAUDE.md §2).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine.init import HOOK_WIRED_COMMAND

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="launcher is POSIX sh; native-Windows users follow the README's WSL2 path",
)

PAYLOAD = '{"tool_name":"Bash","tool_input":{"command":"ls"}}'


def _run(*, bin_on_path: bool, run_id: str, bindir: Path) -> subprocess.CompletedProcess:
    """Run the launcher under /bin/sh, controlling whether the hook is on PATH."""
    base = "/usr/bin:/bin"
    env = {"PATH": f"{bindir}:{base}" if bin_on_path else base}
    if run_id:
        env["GAUNTLET_RUN_ID"] = run_id
    return subprocess.run(
        ["/bin/sh", "-c", HOOK_WIRED_COMMAND],
        input=PAYLOAD, capture_output=True, text=True, env=env,
    )


def _install_fake(bindir: Path, *, stdout: str, exit_code: int) -> None:
    """A stand-in gauntlet-judge-hook: echo stdin (proves passthrough), then exit."""
    bindir.mkdir(parents=True, exist_ok=True)
    hook = bindir / "gauntlet-judge-hook"
    body = "#!/bin/sh\ncat\n"
    if stdout:
        body += f"printf '%s' '{stdout}'\n"
    body += f"exit {exit_code}\n"
    hook.write_text(body)
    hook.chmod(0o755)


def test_present_allow_passes_through_stdin_and_exit0(tmp_path):
    bindir = tmp_path / "bin"
    _install_fake(bindir, stdout="[ALLOW]", exit_code=0)
    p = _run(bin_on_path=True, run_id="run-1", bindir=bindir)
    assert p.returncode == 0
    assert PAYLOAD in p.stdout       # the payload reached the real hook (stdin passthrough)
    assert "[ALLOW]" in p.stdout     # the real hook's stdout reached the CLI


def test_present_deny_exit_code_survives_exec(tmp_path):
    # The single most important property: a DENY (exit 2) must not be swallowed by
    # the launcher's `|| { … }` tail. `exec` replaces the shell, so it cannot be.
    bindir = tmp_path / "bin"
    _install_fake(bindir, stdout='{"deny":1}', exit_code=2)
    p = _run(bin_on_path=True, run_id="run-1", bindir=bindir)
    assert p.returncode == 2
    assert '{"deny":1}' in p.stdout


def test_missing_outside_run_is_silent_exit0(tmp_path):
    # The zero-notice case: a teammate without Gauntlet installed, plain session.
    p = _run(bin_on_path=False, run_id="", bindir=tmp_path / "absent")
    assert p.returncode == 0
    assert p.stdout == ""   # emits no permission decision (stands aside)
    assert p.stderr == ""   # and no 'command not found' notice — the whole point


def test_missing_inside_run_fails_closed(tmp_path):
    # A broken install during an active run must halt, not silently run ungated.
    p = _run(bin_on_path=False, run_id="run-1", bindir=tmp_path / "absent")
    assert p.returncode == 2
    assert "failing closed" in p.stderr
