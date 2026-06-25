"""`gauntlet status` must resolve the run instance through the safe resolver.

Regression for review F-002: the status command read the active-run pointer via
the unchecked ``RunLayout.active_run_dir()`` (a raw concatenation), so a slug or
``active-run.txt`` value escaping the run tree (``../../outside`` or a symlinked
instance dir) let it read a manifest / ``.recovery-intent.json`` outside the
configured run root. It must instead validate the slug + pointer and confirm the
resolved instance stays under the slug dir before reading anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from gauntlet.cli import app

runner = CliRunner()


def _setup_repo(tmp_path: Path) -> Path:
    """A minimal repo with `.gauntlet/config.yaml` + an empty `runs/demo/`."""
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    slug_dir = tmp_path / "runs" / "demo"
    slug_dir.mkdir(parents=True)
    return slug_dir


def _write_manifest(run_dir: Path, *, status: str = "running") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    man = {
        "run_id": "run-x",
        "slug": "demo",
        "branch": "gauntlet/demo",
        "base_branch": "main",
        "pipeline": {"name": "p", "version": 1, "hash": "h"},
        "status": status,
        "steps": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))


def test_status_resolves_a_normal_active_run(tmp_path, monkeypatch):
    slug_dir = _setup_repo(tmp_path)
    _write_manifest(slug_dir / "run-1")
    (slug_dir / "active-run.txt").write_text("run-1\n")

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "demo"])
    assert result.exit_code == 0, result.output
    assert "demo: running" in result.output
    assert "driver: none" in result.output


def test_status_refuses_traversal_in_active_pointer(tmp_path, monkeypatch):
    slug_dir = _setup_repo(tmp_path)
    # An outside run dir with its own intent the command must never read.
    outside = tmp_path / "outside"
    _write_manifest(outside)
    (outside / ".recovery-intent.json").write_text(
        json.dumps({"step_id": "LEAKED", "lock_nonce": "n"})
    )
    (slug_dir / "active-run.txt").write_text("../../outside\n")

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "demo"])
    assert result.exit_code == 1
    assert "unsafe" in result.output.lower()
    assert "LEAKED" not in result.output  # the out-of-tree intent was not read


def test_status_refuses_symlinked_instance_escaping_run_tree(tmp_path, monkeypatch):
    slug_dir = _setup_repo(tmp_path)
    outside = tmp_path / "outside"
    _write_manifest(outside, status="parked")
    (outside / ".recovery-intent.json").write_text(
        json.dumps({"step_id": "LEAKED", "lock_nonce": "n"})
    )
    # A safe-looking segment whose dir is actually a symlink out of the run tree.
    (slug_dir / "run-evil").symlink_to(outside, target_is_directory=True)
    (slug_dir / "active-run.txt").write_text("run-evil\n")

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "demo"])
    assert result.exit_code == 1
    assert "escapes the run tree" in result.output
    assert "LEAKED" not in result.output  # the out-of-tree intent was not read
