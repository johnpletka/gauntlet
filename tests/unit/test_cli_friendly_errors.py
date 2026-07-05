"""CLI error boundary (issue #21): operational failures print one line, not a
traceback; anything unexpected stays loud."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from gauntlet.cli import _friendly_errors, app
from gauntlet.engine.config import ConfigLoadError, ConfigNotFoundError, RunConfig

runner = CliRunner()


# --- the issue's reported case: verbs outside an initialized repo ------------


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "estimating-improvements"],
        ["run", "estimating-improvements"],
        ["resume", "estimating-improvements"],
        ["approve", "estimating-improvements"],
        ["abort", "estimating-improvements"],
        ["report", "estimating-improvements"],
    ],
)
def test_missing_config_is_one_line_not_a_traceback(tmp_path, monkeypatch, argv):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, argv)
    assert result.exit_code == 1
    assert "error: run config not found" in result.output
    assert "gauntlet init" in result.output  # the remedy survives
    assert "Traceback" not in result.output
    assert "FileNotFoundError" not in result.output


def test_malformed_config_is_one_line(tmp_path, monkeypatch):
    # The issue's second case class: settings that exist but don't validate
    # (e.g. bad judge/agent model settings) — pydantic wall becomes one line.
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / ".gauntlet" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("agents:\n  builder:\n    adapter: 42\n")
    result = runner.invoke(app, ["status", "anything"])
    assert result.exit_code == 1
    assert "error: invalid run config at" in result.output
    assert "Traceback" not in result.output


def test_non_mapping_config_is_one_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / ".gauntlet" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("- just\n- a\n- list\n")
    result = runner.invoke(app, ["status", "anything"])
    assert result.exit_code == 1
    assert "error:" in result.output and "YAML mapping" in result.output
    assert "Traceback" not in result.output


# --- config.load raises the typed subclasses (backward-compatible) -----------


def test_config_errors_subclass_prior_types(tmp_path):
    with pytest.raises(FileNotFoundError) as e1:
        RunConfig.load(tmp_path / "nope" / "config.yaml")
    assert isinstance(e1.value, ConfigNotFoundError)

    bad = tmp_path / "config.yaml"
    bad.write_text("agents: [not, a, mapping]\n")
    with pytest.raises(ValueError) as e2:
        RunConfig.load(bad)
    assert isinstance(e2.value, ConfigLoadError)


def test_invalid_yaml_config_is_config_load_error(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("agents: {unclosed\n")
    with pytest.raises(ConfigLoadError) as exc:
        RunConfig.load(bad)
    assert "not valid YAML" in str(exc.value)


# --- the boundary itself ------------------------------------------------------


def test_unexpected_exceptions_stay_loud():
    # Fail closed: a genuine bug must NOT be laundered into a polite line.
    @_friendly_errors
    def boom() -> None:
        raise RuntimeError("a genuine bug")

    with pytest.raises(RuntimeError, match="a genuine bug"):
        boom()


def test_typer_exit_passes_through_untouched():
    @_friendly_errors
    def bail() -> None:
        raise typer.Exit(2)

    with pytest.raises(typer.Exit) as exc:
        bail()
    assert exc.value.exit_code == 2


def test_known_error_maps_to_message_and_exit_1(capsys):
    @_friendly_errors
    def refuse() -> None:
        raise ConfigNotFoundError("run config not found at X")

    with pytest.raises(typer.Exit) as exc:
        refuse()
    assert exc.value.exit_code == 1
    assert "error: run config not found at X" in capsys.readouterr().err


def test_every_command_carries_the_boundary():
    # Regression guard: a new command added without @_friendly_errors would
    # silently regress issue #21. Count registrations vs decorated wrappers.
    import re
    from pathlib import Path

    import gauntlet.cli as cli

    src = Path(cli.__file__).read_text()
    registrations = len(
        re.findall(r"^@(?:app|judge_app|ledger_app|proposals_app)\.command", src, re.M)
    )
    boundaries = src.count("@_friendly_errors")
    assert registrations == boundaries, (
        "every CLI command must be wrapped in @_friendly_errors (issue #21)"
    )
