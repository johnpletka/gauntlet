"""Agent-liveness watchdog (FR-5.3, #103): vanished-child detection end to end.

The #103 defect: an impl-cycle fix agent's process vanished — child gone, its
pipe write-end held open by an escaped fd holder, no EOF — and the driver sat
blocked ~2h at 0.0% CPU while `status` said `agent_silent` with nothing acting
on the signal. These tests pin the fix at every layer:

* the pure decision core (`heartbeat.watchdog_should_fire`) — proof-gated:
  alive child never fires regardless of silence; unreadable liveness never
  fires; dead child within the bound waits; only dead child + empty group +
  silence past the bound fires;
* `run_with_timeout` (buffered and streaming) against a REAL Popen whose child
  exits after detaching a new-session holder of its stdout/stderr pipe — the
  honest reproduction of "child provably gone, stream never EOFs";
* the adapters raise `AgentVanishedError` (never a plain timeout) on the
  vanished flag;
* the orchestrator parks the step INTERRUPTED (halt_reason signal_kill, run
  PARKED) so plain `resume` is the next verb — the existing F-003 plumbing,
  no new state shapes;
* the `status` footer names the agent-silent age with the §4 recover pointer,
  and `status --json` carries the same sampled `agent_output_age_s`;
* `recover` echoes the composite state (papercut: `run status: failed` alone
  misled while the truth was `interrupted`).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import git

from gauntlet.adapters.base import AgentVanishedError
from gauntlet.adapters.claude_code import ClaudeCodeAdapter
from gauntlet.adapters.codex import CodexAdapter
from gauntlet.adapters.process import ProcessOutput, run_with_timeout
from gauntlet.engine import heartbeat as HB
from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.run import RunManager


# =============================================================================
# 1. The pure decision core (heartbeat.watchdog_should_fire)
# =============================================================================


def test_fires_only_on_dead_child_empty_group_past_bound():
    assert HB.watchdog_should_fire(
        child_alive=False, group_alive=False, silence_s=901.0,
        silence_bound_s=900.0,
    )


def test_alive_child_never_fires_regardless_of_silence():
    # Long silent thinking is normal; a live child is NEVER touched (#103).
    assert not HB.watchdog_should_fire(
        child_alive=True, group_alive=False, silence_s=10 ** 9,
        silence_bound_s=1.0,
    )


def test_dead_child_within_bound_waits():
    assert not HB.watchdog_should_fire(
        child_alive=False, group_alive=False, silence_s=899.0,
        silence_bound_s=900.0,
    )


def test_surviving_group_member_holds_the_watchdog_off():
    # Workers can outlive the leader; a non-empty group is not "provably gone".
    assert not HB.watchdog_should_fire(
        child_alive=False, group_alive=True, silence_s=10 ** 9,
        silence_bound_s=1.0,
    )


@pytest.mark.parametrize("child_alive,group_alive", [
    (None, False),   # liveness unreadable
    (False, None),   # group probe unreadable
    (None, None),
])
def test_unreadable_observables_fail_closed_to_waiting(child_alive, group_alive):
    assert not HB.watchdog_should_fire(
        child_alive=child_alive, group_alive=group_alive, silence_s=10 ** 9,
        silence_bound_s=1.0,
    )


def test_no_silence_observation_never_fires():
    assert not HB.watchdog_should_fire(
        child_alive=False, group_alive=False, silence_s=None,
        silence_bound_s=1.0,
    )


@pytest.mark.parametrize("bound", [None, 0.0, -1.0])
def test_disabled_bound_never_fires(bound):
    assert not HB.watchdog_should_fire(
        child_alive=False, group_alive=False, silence_s=10 ** 9,
        silence_bound_s=bound,
    )


# =============================================================================
# 2. run_with_timeout against a real vanishing child
# =============================================================================

# The honest #103 reproduction: the child detaches a holder of its own
# stdout/stderr into a NEW session (so the child's process group empties when
# it exits) and exits cleanly. The holder keeps the pipe write-ends open, so
# EOF never arrives — exactly "child provably gone, stream open, silent".
_VANISH_CHILD = (
    "import subprocess, sys;"
    "h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
    " start_new_session=True);"
    "print('holder', h.pid, flush=True)"
)


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def test_buffered_wait_stops_on_vanished_child_without_eof():
    start = time.monotonic()
    out = run_with_timeout(
        [sys.executable, "-c", _VANISH_CHILD],
        timeout_s=30.0,
        watchdog_silence_s=1.0,
    )
    elapsed = time.monotonic() - start
    assert out.agent_vanished
    assert not out.timed_out  # an interruption, never a budget expiry
    assert out.exit_code == 0  # the child itself exited cleanly
    # Stopped promptly (bound + poll cadence + bounded drain), not at 30s.
    assert elapsed < 15
    # And never before the bound: the dead child got its full grace.
    assert elapsed >= 1.0


def test_streaming_wait_stops_on_vanished_child_and_keeps_lines():
    lines: list[str] = []
    start = time.monotonic()
    out = run_with_timeout(
        [sys.executable, "-c", _VANISH_CHILD],
        timeout_s=30.0,
        sink=lines.append,
        watchdog_silence_s=1.0,
    )
    elapsed = time.monotonic() - start
    holder_pid = int(out.stdout.split()[1])
    try:
        assert out.agent_vanished
        assert not out.timed_out
        assert elapsed < 15
        # Output observed before the vanish is preserved on BOTH surfaces.
        assert any(ln.startswith("holder") for ln in lines)
        assert "holder" in out.stdout
    finally:
        _kill_quietly(holder_pid)  # do not leak the 20s pipe holder


def test_silent_but_alive_child_is_never_interrupted():
    # A child that thinks silently past the bound and then finishes must
    # complete normally — the watchdog acts only on proof of death.
    out = run_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(2); print('done')"],
        timeout_s=30.0,
        watchdog_silence_s=0.5,
    )
    assert not out.agent_vanished
    assert not out.timed_out
    assert out.exit_code == 0
    assert "done" in out.stdout


def test_disabled_watchdog_falls_through_to_the_hard_timeout():
    # watchdog_silence_s=0 disables: the vanished shape is then bounded only
    # by the FR-3.3 wall clock, byte-for-byte the pre-#103 behavior.
    out = run_with_timeout(
        [sys.executable, "-c", _VANISH_CHILD],
        timeout_s=2.0,
        watchdog_silence_s=0,
    )
    assert out.timed_out
    assert not out.agent_vanished


# =============================================================================
# 3. Adapter surface: the vanished flag raises AgentVanishedError
# =============================================================================


def _vanished_output(argv0: str) -> ProcessOutput:
    return ProcessOutput(
        argv=[argv0], stdout="", stderr="", exit_code=0,
        duration_s=42.0, timed_out=False, agent_vanished=True,
    )


def test_claude_adapter_raises_agent_vanished(monkeypatch):
    monkeypatch.setattr(
        "gauntlet.adapters.claude_code.run_with_timeout",
        lambda argv, **kw: _vanished_output("claude"),
    )
    with pytest.raises(AgentVanishedError) as exc:
        ClaudeCodeAdapter(model="haiku").run("ping")
    assert "watchdog" in str(exc.value)
    assert "#103" in str(exc.value)
    assert exc.value.partial is not None  # checkpointable evidence retained


def test_codex_adapter_raises_agent_vanished(monkeypatch):
    monkeypatch.setattr(
        "gauntlet.adapters.codex.run_with_timeout",
        lambda argv, **kw: _vanished_output("codex"),
    )
    with pytest.raises(AgentVanishedError) as exc:
        CodexAdapter(model="gpt-5").run("ping")
    assert "watchdog" in str(exc.value)
    assert exc.value.partial is not None


def test_profile_knob_arms_the_adapter_bound():
    # The engine-level guard is stripped from adapter kwargs (like
    # step_timeout_s) and armed onto the attribute instead.
    from gauntlet.engine.config import AgentProfile

    profile = AgentProfile.model_validate(
        {"adapter": "claude-code", "agent_silent_timeout_s": 120.0}
    )
    assert "agent_silent_timeout_s" not in profile._adapter_kwargs()
    adapter = profile.build_adapter()
    assert adapter.watchdog_silence_s is None  # constructor default: engine's
    adapter.watchdog_silence_s = profile.agent_silent_timeout_s
    assert adapter.watchdog_silence_s == 120.0


# =============================================================================
# 4. Orchestrator: AgentVanishedError parks the step INTERRUPTED
# =============================================================================

_CONFIG = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

_PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""


def _seed(repo: Path):
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(_CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed config")
    git(repo, "checkout", "-qb", "gauntlet/demo")
    slug_dir = repo / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")
    (run_dir / "pipeline.yaml").write_text(_PIPELINE)
    (slug_dir / ".gitignore").write_text(".gitignore\nactive-run.txt\n")
    (slug_dir / "active-run.txt").write_text("run-1")
    _, phash = load_pipeline(run_dir / "pipeline.yaml")
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash=phash),
        status=M.RUN_RUNNING, steps=[],
    )
    man.write_atomic(run_dir / "manifest.json")
    return RunManager(repo), run_dir


class VanishingAdapter:
    """Raises the watchdog error, as a real adapter does on a vanished child."""

    name = "fake"
    from gauntlet.adapters.base import AdapterCapabilities as _Caps

    capabilities = _Caps(repo_write=True, structured_output="native", resume=True)
    timeout_s = 600.0
    watchdog_silence_s = None

    def __init__(self):
        self.calls = 0

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls += 1
        raise AgentVanishedError(
            "agent-liveness watchdog (FR-5.3, #103): the claude child process "
            "was provably gone with its output stream still open and silent "
            "past the 900s bound; stopped waiting after 942s."
        )


def test_vanished_agent_parks_step_interrupted_for_plain_resume(fixture_repo):
    mgr, run_dir = _seed(fixture_repo)
    adapter = VanishingAdapter()
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    )
    assert status == M.RUN_PARKED  # INTERRUPTED parks (F-003), never FAILED
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.INTERRUPTED
    assert rec.halt_reason == M.HALT_REASON_SIGNAL_KILL
    assert "watchdog" in (rec.notes or "")
    assert "resume" in (rec.notes or "")  # the next verb is named
    assert adapter.calls == 1


def test_vanished_park_resumes_clean_step_via_plain_resume(fixture_repo):
    # The promised path: interrupted → plain resume re-runs (the agent left no
    # partial edits here, so the F-003 disposition re-enters cleanly).
    from conftest import FakeAdapter

    mgr, run_dir = _seed(fixture_repo)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: VanishingAdapter()
    ) == M.RUN_PARKED
    recovered = FakeAdapter(writes={"done.py": "ok\n"})
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: recovered
    ) == M.RUN_DONE
    assert recovered.calls  # the re-run actually happened


# =============================================================================
# 5. Status surfacing: agent-silent age in the footer and --json
# =============================================================================

T0 = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _man_running() -> Manifest:
    return Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_RUNNING,
        steps=[StepRecord(id="implement", type="agent_task", status=M.RUNNING)],
    )


def _write_heartbeat(run_dir: Path, mono: float, wall: datetime) -> None:
    import json

    (run_dir / HB.HEARTBEAT_FILENAME).write_text(json.dumps({
        "monotonic_s": mono,
        "wallclock_utc": HB.format_wallclock(wall),
        "pid": os.getpid(),
    }))


def _agent_silent_view(tmp_path: Path) -> dict:
    # Fresh heartbeat (driver writing) + stale step events → agent_silent.
    _write_heartbeat(tmp_path, 100.0, T0)
    steps_dir = tmp_path / "steps" / "implement"
    steps_dir.mkdir(parents=True)
    events = steps_dir / "events.jsonl"
    events.write_text('{"e":1}\n')
    old = (T0 - timedelta(minutes=34)).timestamp()
    os.utime(events, (old, old))
    return op.compute_suspension_view(
        _man_running(), tmp_path, op.LIVENESS_ALIVE, now=T0,
        agent_silence_s=300.0,
    )


def test_suspension_view_carries_the_sampled_agent_output_age(tmp_path):
    view = _agent_silent_view(tmp_path)
    assert view["classification"] == HB.STALL_AGENT_SILENT
    # The SAME sampled input the classification consumed, surfaced (#103).
    assert view["agent_output_age_s"] == pytest.approx(34 * 60, abs=2)


def test_footer_names_agent_silent_age_and_the_recover_verb(tmp_path):
    view = _agent_silent_view(tmp_path)
    driver = op.DriverInfo(op.LIVENESS_ALIVE, 4242, "host", "s")
    rstate = op.compute_run_state(_man_running(), driver.state)
    text = "\n".join(
        op.render_footer(driver, rstate, suspension=view, slug="demo")
    )
    assert "agent silent: no adapter output for 34m" in text
    assert "see §4 recover (`gauntlet recover demo`)" in text


def test_footer_has_no_agent_silent_line_when_healthy(tmp_path):
    _write_heartbeat(tmp_path, 100.0, T0)
    view = op.compute_suspension_view(
        _man_running(), tmp_path, op.LIVENESS_ALIVE, now=T0
    )
    assert view["classification"] is None
    driver = op.DriverInfo(op.LIVENESS_ALIVE, 4242, "host", "s")
    rstate = op.compute_run_state(_man_running(), driver.state)
    lines = op.render_footer(driver, rstate, suspension=view, slug="demo")
    assert not any(ln.startswith("agent silent:") for ln in lines)


def test_status_payload_validates_with_agent_output_age(tmp_path):
    view = _agent_silent_view(tmp_path)
    man = _man_running()
    driver = op.DriverInfo(op.LIVENESS_ALIVE, 4242, "host", "2026-08-10T12-00-00")
    rstate = op.compute_run_state(man, driver.state)
    payload = op.status_payload(
        man, driver, rstate, None,
        run_root=tmp_path, run_instance_dir=tmp_path, suspension=view,
    )  # raises StatusContractError on any schema drift
    assert payload["suspension"]["agent_output_age_s"] == pytest.approx(
        34 * 60, abs=2
    )


# =============================================================================
# 6. `recover` echoes the composite truth (papercut #103-1)
# =============================================================================

runner = CliRunner()


def _seed_recovered_run(root: Path) -> None:
    (root / ".gauntlet").mkdir()
    (root / ".gauntlet" / "config.yaml").write_text(_CONFIG)
    slug_dir = root / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        # The post-recover shape from the #103 run: raw run_status `failed`,
        # step INTERRUPTED — the composite truth is `interrupted`.
        status=M.RUN_FAILED,
        steps=[StepRecord(
            id="implement", type="agent_task", status=M.INTERRUPTED,
            halt_reason=M.HALT_REASON_SIGNAL_KILL, notes="recovered",
        )],
    )
    man.write_atomic(run_dir / "manifest.json")
    (slug_dir / "active-run.txt").write_text("run-1")


def test_recover_prints_composite_state_not_just_raw_status(tmp_path, monkeypatch):
    from gauntlet.cli import app

    monkeypatch.chdir(tmp_path)
    _seed_recovered_run(tmp_path)
    monkeypatch.setattr(
        RunManager, "recover", lambda self, slug, **kw: M.RUN_FAILED
    )
    result = runner.invoke(app, ["recover", "demo"])
    assert result.exit_code == 0
    assert "run status: failed" in result.stdout  # the raw status still shown
    # ... but the composite truth travels with it, plus the next verb (#103).
    assert "state: interrupted" in result.stdout
    assert "gauntlet resume demo" in result.stdout


def test_refused_recover_prints_no_state_line(tmp_path, monkeypatch):
    from gauntlet.cli import app
    from gauntlet.engine.run import RecoverRefused

    monkeypatch.chdir(tmp_path)
    _seed_recovered_run(tmp_path)

    def refuse(self, slug, **kw):
        raise RecoverRefused("no live driver")

    monkeypatch.setattr(RunManager, "recover", refuse)
    result = runner.invoke(app, ["recover", "demo"])
    assert result.exit_code == 1
    assert "state:" not in result.stdout
