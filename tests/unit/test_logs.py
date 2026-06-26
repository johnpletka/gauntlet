"""`gauntlet logs` — read-only evidence access (P2, FR-3).

Adversarial coverage of the assumption that failed/halted/interrupted-step
evidence is reachable in one read-only command from the known layout,
deterministically (never by dir mtime) and without crashing on missing/malformed
artifacts. Every test is read-only; the containment tests assert no out-of-tree
read and that nothing under the run dir is created or modified.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gauntlet.cli import app
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


def _manifest(inst: Path, steps: list[dict], *, status: str = "failed") -> None:
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


def _step(id: str, type: str = "agent_task", status: str = "done", **kw) -> dict:
    return {"id": id, "type": type, "status": status, **kw}


def _transcript(inst: Path, leaf: str, body: str) -> Path:
    d = inst / "steps" / leaf
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.md").write_text(body)
    return d


# --- default-step selection (FR-3.1a): last non-done, highest iteration ------
def test_default_step_is_last_non_done_highest_iteration(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("a", status="done"),
        _step("impl", iteration="0", status="done"),
        _step("impl", iteration="1", status="failed"),
    ])
    _transcript(inst, "a", "alpha done\n")
    _transcript(inst, "impl.0", "first iteration\n")
    _transcript(inst, "impl.1", "FAILING ITERATION EVIDENCE\n")
    # Touch the *done* dir last so mtime ordering would mis-select it.
    os.utime(inst / "steps" / "a", None)

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert "step: impl.1 (failed)" in result.output
    assert "FAILING ITERATION EVIDENCE" in result.output
    assert "alpha done" not in result.output  # not the mtime-newest dir


def test_default_step_all_done_shows_last_done(tmp_path, monkeypatch):
    inst = _instance(tmp_path, name="run-2026-06-25T17-00-00")
    _manifest(inst, [
        _step("a", status="done"),
        _step("commit", type="commit", status="done"),
    ], status="done")
    _transcript(inst, "a", "alpha\n")
    _transcript(inst, "commit", "committed\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert "step: commit (done)" in result.output


# --- transcript dir + events path are surfaced -------------------------------
def test_prints_step_dir_and_events_path(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    _transcript(inst, "impl", "evidence here\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert str(inst) in result.output
    assert (inst / "steps" / "impl" / "events.jsonl").as_posix() in result.output
    assert "evidence here" in result.output


# --- FR-3.1b: default tail policy (last 200 lines) ---------------------------
def test_long_transcript_tailed_to_200(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    body = "\n".join(f"line-{i}" for i in range(500)) + "\n"
    _transcript(inst, "impl", body)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert "line-499" in result.output  # the tail is present
    assert "line-299" not in result.output  # only the last 200 (300..499)
    assert "line-300" in result.output
    assert "(last 200 lines)" in result.output


def test_short_transcript_shown_in_full(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    body = "\n".join(f"line-{i}" for i in range(10)) + "\n"
    _transcript(inst, "impl", body)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert "line-0" in result.output and "line-9" in result.output
    assert "(last 200 lines)" not in result.output


# --- FR-3.1c: missing / unreadable transcript → notice, exit 0 ---------------
def test_absent_transcript_prints_notice_exit_0(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="interrupted")])
    (inst / "steps" / "impl").mkdir(parents=True)  # dir exists, no transcript.md
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert "transcript absent/unreadable (step status: interrupted)" in result.output
    assert "events.jsonl" in result.output  # events path still named


def test_unreadable_transcript_prints_notice_exit_0(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    d = inst / "steps" / "impl"
    d.mkdir(parents=True)
    (d / "transcript.md").mkdir()  # a directory where a file is expected → OSError
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert "transcript absent/unreadable" in result.output


# --- FR-3.2: --step selection + unknown-step error ---------------------------
def test_explicit_step_top_level(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("a", status="done"), _step("impl", status="failed")])
    _transcript(inst, "a", "ALPHA-BODY\n")
    _transcript(inst, "impl", "impl body\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", "a"])
    assert result.exit_code == 0, result.output
    assert "step: a (done)" in result.output
    assert "ALPHA-BODY" in result.output


def test_unknown_step_errors_listing_real_leaves(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("a", status="done"),
        _step("cyc", type="adversarial_cycle", iteration="0", status="failed",
              metrics={"rounds": 1}),
    ])
    _transcript(inst, "a", "alpha\n")
    cyc = inst / "steps" / "cyc.0"
    (cyc / "r1-review").mkdir(parents=True)
    (cyc / "r1-fix").mkdir()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", "does-not-exist"])
    assert result.exit_code == 1
    out = result.output
    assert "unknown step" in out
    # Real leaves listed: the top-level ids and the composite role sub-leaves.
    assert "a" in out and "cyc.0" in out
    assert "cyc.0/r1-review" in out and "cyc.0/r1-fix" in out


# --- composite-step leaf resolution via the CLI (metadata-driven) ------------
def test_cycle_default_resolves_highest_round_most_recent_role(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("cyc", type="adversarial_cycle", iteration="0", status="failed",
              metrics={"rounds": 2}),
    ])
    cyc = inst / "steps" / "cyc.0"
    for role in ("r1-review", "r2-review", "r2-fix"):
        (cyc / role).mkdir(parents=True)
    (cyc / "r2-confirm").mkdir()
    (cyc / "r2-confirm" / "transcript.md").write_text("R2 CONFIRM EVIDENCE\n")
    # Make an older round's dir mtime-newest; resolution must ignore mtime.
    os.utime(cyc / "r1-review", None)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert "R2 CONFIRM EVIDENCE" in result.output
    assert "r2-confirm" in result.output


def test_explicit_step_composite_sub_leaf(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("cyc", type="adversarial_cycle", iteration="0", status="failed",
              metrics={"rounds": 2}),
    ])
    cyc = inst / "steps" / "cyc.0"
    (cyc / "r1-fix").mkdir(parents=True)
    (cyc / "r1-fix" / "transcript.md").write_text("R1 FIX NESTED\n")
    (cyc / "r2-confirm").mkdir()
    (cyc / "r2-confirm" / "transcript.md").write_text("default r2\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", "cyc.0/r1-fix"])
    assert result.exit_code == 0, result.output
    assert "R1 FIX NESTED" in result.output
    assert "default r2" not in result.output


def test_explicit_sub_leaf_missing_dir_errors(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("cyc", type="adversarial_cycle", iteration="0", status="failed",
              metrics={"rounds": 1}),
    ])
    (inst / "steps" / "cyc.0" / "r1-review").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", "cyc.0/r9-fix"])
    assert result.exit_code == 1
    assert "unknown step" in result.output


def test_explicit_sub_leaf_finding_id_three_segments(tmp_path, monkeypatch):
    # The documented deepest leaf: <cycle>/r1-triage/<finding-id> (3 segments).
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("cyc", type="adversarial_cycle", iteration="0", status="failed",
              metrics={"rounds": 1}),
    ])
    triage = inst / "steps" / "cyc.0" / "r1-triage" / "F-001"
    triage.mkdir(parents=True)
    (triage / "transcript.md").write_text("FINDING ONE TRIAGE\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", "cyc.0/r1-triage/F-001"])
    assert result.exit_code == 0, result.output
    assert "FINDING ONE TRIAGE" in result.output


# --- F-003: nested selectors bounded to the composite leaf grammar -----------
def test_nested_selector_under_non_composite_rejected(tmp_path, monkeypatch):
    # An atomic step has no addressable sub-leaves; a nested selector is unknown
    # even when the directory happens to exist on disk.
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    (inst / "steps" / "impl" / "sub").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", "impl/sub"])
    assert result.exit_code == 1
    assert "unknown step" in result.output


def test_nested_selector_too_deep_rejected(tmp_path, monkeypatch):
    # A composite leaf bounds at role + one child (3 segments); a fourth segment
    # is unknown even when the directory exists.
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("cyc", type="adversarial_cycle", iteration="0", status="failed",
              metrics={"rounds": 1}),
    ])
    deep = inst / "steps" / "cyc.0" / "r1-triage" / "F-001" / "extra"
    deep.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["logs", "demo", "--step", "cyc.0/r1-triage/F-001/extra"]
    )
    assert result.exit_code == 1
    assert "unknown step" in result.output


# --- F-001: composite-leaf enumeration is contained before any read ----------
def test_unknown_step_does_not_enumerate_symlinked_composite_dir(tmp_path, monkeypatch):
    # A composite step dir that is a symlink escaping the run tree must never be
    # enumerated for the available-steps message — no out-of-tree names leak.
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("cyc", type="adversarial_cycle", iteration="0", status="failed",
              metrics={"rounds": 1}),
    ])
    outside = tmp_path / "outside"
    (outside / "r1-secret").mkdir(parents=True)
    steps = inst / "steps"
    steps.mkdir()
    (steps / "cyc.0").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", "does-not-exist"])
    assert result.exit_code == 1
    out = result.output
    assert "unknown step" in out
    assert "cyc.0" in out  # the top-level id itself is fine to name
    assert "r1-secret" not in out  # the out-of-tree contents are never listed


def test_unknown_step_does_not_follow_symlinked_role_dir(tmp_path, monkeypatch):
    # A symlinked role inside a contained composite dir must not be followed:
    # neither the role nor its children appear in the available-steps message.
    inst = _instance(tmp_path)
    _manifest(inst, [
        _step("cyc", type="adversarial_cycle", iteration="0", status="failed",
              metrics={"rounds": 1}),
    ])
    cyc = inst / "steps" / "cyc.0"
    (cyc / "r1-review").mkdir(parents=True)  # a legit, contained role
    outside = tmp_path / "outside"
    (outside / "secret-finding").mkdir(parents=True)
    (cyc / "r1-escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", "does-not-exist"])
    assert result.exit_code == 1
    out = result.output
    assert "cyc.0/r1-review" in out  # the real role is listed
    assert "r1-escape" not in out  # the symlinked role is not followed
    assert "secret-finding" not in out  # its out-of-tree children never enumerated


# --- F-002: missing / malformed manifest → controlled LogsError, exit 1 ------
def test_missing_manifest_errors(tmp_path, monkeypatch):
    inst = _instance(tmp_path)  # instance dir + active-run.txt, but no manifest.json
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 1
    assert "cannot load manifest" in result.output


def test_invalid_json_manifest_errors(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    (inst / "manifest.json").write_text("{not valid json")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 1
    assert "cannot load manifest" in result.output


def test_schema_invalid_manifest_errors(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    # Well-formed JSON, but missing the manifest's required fields.
    (inst / "manifest.json").write_text(json.dumps({"slug": "demo"}))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 1
    assert "cannot load manifest" in result.output


# --- active-run.txt naming a missing instance → error listing instances ------
def test_active_pointer_to_missing_instance_errors(tmp_path, monkeypatch):
    slug_dir = tmp_path / "runs" / "demo"
    (slug_dir / "run-2026-06-25T10-00-00").mkdir(parents=True)
    (slug_dir / "active-run.txt").write_text("run-gone\n")
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 1
    assert "run-2026-06-25T10-00-00" in result.output  # available listed


# --- FR-3.3: read-only + containment -----------------------------------------
def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_logs_writes_nothing(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    _transcript(inst, "impl", "evidence\n")
    before = _snapshot(tmp_path / "runs")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 0, result.output
    assert _snapshot(tmp_path / "runs") == before  # zero mutation


@pytest.mark.parametrize("bad", ["../escape", "..", "a/../../b", "foo\x00bar"])
def test_path_traversal_step_rejected(tmp_path, monkeypatch, bad):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    _transcript(inst, "impl", "evidence\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo", "--step", bad])
    assert result.exit_code == 1


def test_symlink_escape_step_dir_refused_no_read(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    # An out-of-tree target whose transcript must never be read.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "transcript.md").write_text("LEAKED OUT OF TREE\n")
    steps = inst / "steps"
    steps.mkdir()
    (steps / "impl").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 1
    assert "escapes the run tree" in result.output
    assert "LEAKED OUT OF TREE" not in result.output


def test_symlink_escape_transcript_file_refused_no_read(tmp_path, monkeypatch):
    inst = _instance(tmp_path)
    _manifest(inst, [_step("impl", status="failed")])
    outside = tmp_path / "secret.md"
    outside.write_text("LEAKED FILE CONTENT\n")
    d = inst / "steps" / "impl"
    d.mkdir(parents=True)
    (d / "transcript.md").symlink_to(outside)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "demo"])
    assert result.exit_code == 1
    assert "escapes the run tree" in result.output
    assert "LEAKED FILE CONTENT" not in result.output


# --- direct resolve_logs unit checks (no CLI) --------------------------------
def test_resolve_logs_unsafe_slug_raises(tmp_path):
    with pytest.raises(op.LogsError):
        op.resolve_logs(tmp_path, tmp_path / "..", "../evil")
