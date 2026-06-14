"""Regression: `gauntlet version` must match the installed package metadata.

Guards against the drift that shipped in 0.1.1, where __version__ was a
hardcoded literal in src/gauntlet/__init__.py and fell behind the version in
pyproject.toml, so `gauntlet version` reported a stale number.
"""

from importlib.metadata import version

from typer.testing import CliRunner

import gauntlet
from gauntlet.cli import app

runner = CliRunner()


def test_dunder_version_matches_installed_metadata():
    # __version__ is derived from dist metadata, never a separate literal.
    assert gauntlet.__version__ == version("gauntlet-spec")


def test_version_command_reports_installed_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"gauntlet {version('gauntlet-spec')}"
