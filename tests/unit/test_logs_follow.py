"""`gauntlet logs --follow` — live offset-tail of the current step (P3, FR-3).

Adversarial coverage of the assumption that an agent or human can actively
monitor a live step from the CLI: incremental output for a running step, a clean
exit at step end (and on SIGINT), no dropped tail across the terminal-status
flip, a one-shot degrade for a finished step (no hang), and the inherited
read-only containment guarantees (redacted bytes only, traversal `--step`
rejected). Every test is read-only and drives the poll loop to a deterministic
end via injected `sleep`/`max_polls`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gauntlet.cli import app
from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op

runner = CliRunner()


# --- fixtures / builders -----------------------------------------------------
def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    return tmp_path


def _instance(tmp_path: Path, *, name: str = "run-2026-06-25T16-41-22") -> Path:
    _repo(tmp_path)
    slug_dir = tmp_path / "runs" / "demo"
    inst = slug_dir / name
    inst.mkdir(parents=True)
    (slug_dir / "active-run.txt").write_text(name + "\n")
    return inst


def _write_manifest(inst: Path, steps: list[dict], *, status: str = "running") -> None:
    man = {
        "run_id": inst.name,
        "slug": "demo",
        "branch": "gauntlet/demo",
        "base_branch": "main",
        "pipeline": {"name": "p", "version": 1, "hash": "h"},
        "status": status,
        "steps": steps,
    }
    (inst / "manifest.json").write_text(json.dumps(man))


def _step(id: str, *, status: str = "running") -> dict:
    return {"id": id, "type": "agent_task", "status": status}


def _events_path(inst: Path, leaf: str = "impl") -> Path:
    d = inst / "steps" / leaf
    d.mkdir(parents=True, exist_ok=True)
    return d / "events.jsonl"


def _follow(inst: Path, tmp_path: Path, **kw) -> tuple[list[str], op.FollowResult]:
    """Drive ``follow_logs`` against the demo run, collecting emitted chunks."""
    out: list[str] = []
    fr = op.follow_logs(
        tmp_path / "runs",
        tmp_path / "runs" / "demo",
        "demo",
        emit=out.append,
        **kw,
    )
    return out, fr


# --- FR-3.1: incremental output, then a clean exit at step end ---------------
def test_running_step_streams_appended_events_then_exits_at_step_end(tmp_path):
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="running")])
    ev = _events_path(inst)
    ev.write_text('{"e":1}\n')

    # Each poll's sleep advances the world: append another event, and on the
    # last tick flip the step (and run) to done so the loop exits.
    appends = ['{"e":2}\n', '{"e":3}\n']

    def fake_sleep(_interval):
        if appends:
            with ev.open("a") as fh:
                fh.write(appends.pop(0))
        else:
            _write_manifest(inst, [_step("impl", status="done")], status="done")

    out, fr = _follow(inst, tmp_path, sleep=fake_sleep, max_polls=10)

    assert "".join(out) == '{"e":1}\n{"e":2}\n{"e":3}\n'
    assert fr.followed is True
    assert fr.interrupted is False
    assert fr.final_status == M.DONE


# --- FR-3.1: clean exit on simulated SIGINT ----------------------------------
def test_sigint_stops_cleanly_after_emitting_so_far(tmp_path):
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="running")])
    ev = _events_path(inst)
    ev.write_text('{"e":1}\n')

    def fake_sleep(_interval):
        # The operator hits Ctrl-C while the step is still running.
        raise KeyboardInterrupt

    out, fr = _follow(inst, tmp_path, sleep=fake_sleep, max_polls=10)

    assert "".join(out) == '{"e":1}\n'  # the pre-SIGINT content was flushed
    assert fr.interrupted is True
    assert fr.followed is True


# --- FR-3.1: no dropped tail across the terminal-status flip -----------------
def test_no_dropped_tail_when_final_events_land_as_status_flips(tmp_path):
    """The manifest can flip to terminal while bytes flushed at step end still
    sit unread past the offset. The loop must drain *after* observing terminal,
    so those last bytes are never lost."""
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="running")])
    ev = _events_path(inst)
    ev.write_text('{"e":1}\n')

    def fake_sleep(_interval):
        # In the same window: write the FINAL events *and* flip the manifest to
        # done. A naive "break immediately on terminal status" would lose these
        # trailing bytes; the post-status drain must catch them.
        with ev.open("a") as fh:
            fh.write('{"e":2}\n{"e":3}\n')
        _write_manifest(inst, [_step("impl", status="done")], status="done")

    out, fr = _follow(inst, tmp_path, sleep=fake_sleep, max_polls=10)

    # Poll 1 drains "{e:1}" (running); sleep writes the tail + flips done; poll 2
    # observes done and still drains the tail before exiting.
    assert "".join(out) == '{"e":1}\n{"e":2}\n{"e":3}\n'
    assert fr.final_status == M.DONE
    assert fr.interrupted is False


# --- FR-3.2: a finished step degrades to a one-shot dump + exit (no hang) -----
def test_finished_step_dumps_once_and_exits_without_following(tmp_path):
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="done")], status="done")
    ev = _events_path(inst)
    ev.write_text('{"e":1}\n{"e":2}\n')

    slept = []
    out, fr = _follow(inst, tmp_path, sleep=lambda i: slept.append(i), max_polls=10)

    assert "".join(out) == '{"e":1}\n{"e":2}\n'
    assert fr.followed is False  # never entered the live poll loop
    assert slept == []  # one-shot: it never sleeps/polls
    assert fr.final_status == M.DONE


def test_finished_step_with_no_events_file_dumps_nothing_and_exits(tmp_path):
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="done")], status="done")
    _events_path(inst)  # create the step dir but no events.jsonl

    out, fr = _follow(inst, tmp_path, max_polls=10)

    assert out == []  # absent file → nothing to dump, no crash, no hang
    assert fr.followed is False


# --- a `running` step whose first line has not landed yet --------------------
def test_running_step_with_no_first_line_yet_then_event_arrives(tmp_path):
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="running")])
    ev_dir = inst / "steps" / "impl"
    ev_dir.mkdir(parents=True)
    ev = ev_dir / "events.jsonl"  # not created yet

    def fake_sleep(_interval):
        if not ev.exists():
            ev.write_text('{"e":1}\n')  # producer creates the file mid-run
        else:
            _write_manifest(inst, [_step("impl", status="done")], status="done")

    out, fr = _follow(inst, tmp_path, sleep=fake_sleep, max_polls=10)

    assert "".join(out) == '{"e":1}\n'
    assert fr.followed is True


# --- FR-3.3: reads only redacted on-disk bytes; traversal `--step` rejected ---
def test_follow_emits_only_the_on_disk_redacted_bytes(tmp_path):
    """`--follow` reads the same redacted `events.jsonl` on disk — never the raw
    pipe. A secret that was redacted before reaching disk stays redacted here."""
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="done")], status="done")
    ev = _events_path(inst)
    # The on-disk file is already redacted (P2 contract); follow only mirrors it.
    ev.write_text('{"text":"token=[REDACTED]"}\n')

    out, _ = _follow(inst, tmp_path, max_polls=10)

    joined = "".join(out)
    assert "[REDACTED]" in joined
    assert "sk-secret" not in joined  # nothing un-redacted is ever read


def test_traversal_step_is_rejected(tmp_path):
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="running")])
    _events_path(inst)
    with pytest.raises(op.LogsError):
        op.follow_logs(
            tmp_path / "runs",
            tmp_path / "runs" / "demo",
            "demo",
            step="../../../etc",
            emit=lambda _t: None,
            max_polls=1,
        )


def test_events_replaced_with_escaping_symlink_mid_follow_is_refused(tmp_path):
    """TOCTOU (F-001): containment is checked once at resolve, but the live file
    can be created/replaced between polls. If `events.jsonl` is swapped for a
    symlink escaping the run tree after follow starts, the next read must fail
    closed — never tail the out-of-tree target (FR-3.3)."""
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="running")])
    ev = _events_path(inst)
    ev.write_text('{"e":1}\n')
    # A target outside the run tree the swapped-in symlink will point at.
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"secret":"sk-leak"}\n')

    def fake_sleep(_interval):
        ev.unlink()
        ev.symlink_to(outside)

    out: list[str] = []
    with pytest.raises(op.LogsError):
        op.follow_logs(
            tmp_path / "runs",
            tmp_path / "runs" / "demo",
            "demo",
            emit=out.append,
            sleep=fake_sleep,
            max_polls=10,
        )
    # Poll 1 flushed the legit in-tree byte; the out-of-tree content was never read.
    assert "".join(out) == '{"e":1}\n'
    assert "sk-leak" not in "".join(out)


def test_corrupt_manifest_mid_follow_fails_closed(tmp_path):
    """F-002: a manifest that turns invalid after follow starts must fail closed
    (LogsError), not be mapped to `running` and polled forever (fail-closed)."""
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="running")])
    ev = _events_path(inst)
    ev.write_text('{"e":1}\n')

    def fake_sleep(_interval):
        (inst / "manifest.json").write_text("{not valid json")  # invalid → ValueError

    out: list[str] = []
    with pytest.raises(op.LogsError):
        op.follow_logs(
            tmp_path / "runs",
            tmp_path / "runs" / "demo",
            "demo",
            emit=out.append,
            sleep=fake_sleep,
            max_polls=10,
        )
    assert "".join(out) == '{"e":1}\n'  # the pre-corruption tail still flushed


def test_removed_manifest_mid_follow_fails_closed(tmp_path):
    """F-002 (OSError branch): a manifest removed after follow starts is an
    integrity failure, not a still-running step — fail closed rather than hang."""
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="running")])
    ev = _events_path(inst)
    ev.write_text('{"e":1}\n')

    def fake_sleep(_interval):
        (inst / "manifest.json").unlink()  # gone → OSError on reload

    with pytest.raises(op.LogsError):
        op.follow_logs(
            tmp_path / "runs",
            tmp_path / "runs" / "demo",
            "demo",
            emit=lambda _t: None,
            sleep=fake_sleep,
            max_polls=10,
        )


# --- CLI surface: `--follow` wires through and exits 0 -----------------------
def test_cli_follow_streams_and_exits_zero(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="done")], status="done")
    ev = _events_path(inst)
    ev.write_text('{"e":1}\n')

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--follow"])

    assert result.exit_code == 0, result.output
    assert '{"e":1}' in result.output


def test_cli_follow_short_flag_is_accepted(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _write_manifest(inst, [_step("impl", status="done")], status="done")
    ev = _events_path(inst)
    ev.write_text('{"e":7}\n')

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "-f"])

    assert result.exit_code == 0, result.output
    assert '{"e":7}' in result.output
