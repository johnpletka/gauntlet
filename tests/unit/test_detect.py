"""Test-command detection for `gauntlet init` (issue #18).

Covers each recognised stack, the multi-module ambiguity that motivated the
issue (backend + frontend), the no-tests-yet case, and the fail-closed
placeholder for unrecognised repos.
"""

from __future__ import annotations

import json

import pytest

from gauntlet.engine.detect import (
    PLACEHOLDER_MARKER,
    detect_test_command,
    is_placeholder_command,
)


def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_python_pytest_with_uv(tmp_path):
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path / "uv.lock")
    _write(tmp_path / "tests/test_thing.py", "def test_x():\n    assert True\n")
    d = detect_test_command(tmp_path)
    assert d.detected
    assert d.command == "uv run pytest"


def test_python_pytest_without_uv_uses_bare_pytest(tmp_path):
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path / "tests/test_thing.py", "def test_x():\n    assert True\n")
    d = detect_test_command(tmp_path)
    assert d.detected
    assert d.command == "pytest"


def test_python_without_tests_is_detected_but_noted(tmp_path):
    # Concern #3: a fresh Python repo with no tests still gets the right command,
    # but the operator is warned the gate will fail until tests exist.
    _write(tmp_path / "pyproject.toml", "[tool.uv]\n")
    d = detect_test_command(tmp_path)
    assert d.detected
    assert d.command == "uv run pytest"
    assert "no tests found" in d.note.lower()


def test_node_with_test_script(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"test": "jest"}}))
    d = detect_test_command(tmp_path)
    assert d.detected
    assert d.command == "npm test"


def test_node_package_manager_from_lockfile(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"test": "vitest"}}))
    _write(tmp_path / "pnpm-lock.yaml")
    d = detect_test_command(tmp_path)
    assert d.command == "pnpm test"


def test_node_init_stub_test_script_is_not_real_tests(tmp_path):
    _write(
        tmp_path / "package.json",
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}),
    )
    d = detect_test_command(tmp_path)
    assert d.detected  # still a node project
    assert "no tests found" in d.note.lower()


def test_rust(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    d = detect_test_command(tmp_path)
    assert d.detected
    assert d.command == "cargo test"


def test_go(tmp_path):
    _write(tmp_path / "go.mod", "module example.com/x\n")
    d = detect_test_command(tmp_path)
    assert d.detected
    assert d.command == "go test ./..."


def test_multi_module_backend_frontend_is_ambiguous(tmp_path):
    # Concern #2: a repo with a Python backend and a Node frontend has no single
    # test command — gauntlet must not silently pick one.
    _write(tmp_path / "backend/pyproject.toml", "[tool.uv]\n")
    _write(tmp_path / "frontend/package.json", json.dumps({"scripts": {"test": "jest"}}))
    d = detect_test_command(tmp_path)
    assert d.status == "ambiguous"
    assert is_placeholder_command(d.command)
    assert "backend" in d.note and "frontend" in d.note
    # The candidate commands are cd-prefixed into their modules.
    candidates = {s.command for s in d.stacks}
    assert "cd backend && uv run pytest" in candidates
    assert "cd frontend && npm test" in candidates


def test_unrecognised_repo_yields_placeholder(tmp_path):
    # Concern #1: a non-Python repo (or empty one) gets a fail-closed placeholder.
    _write(tmp_path / "README.md", "# just docs\n")
    d = detect_test_command(tmp_path)
    assert d.status == "none"
    assert is_placeholder_command(d.command)
    assert PLACEHOLDER_MARKER in d.command


def test_single_nested_module_is_cd_prefixed(tmp_path):
    _write(tmp_path / "service/go.mod", "module x\n")
    d = detect_test_command(tmp_path)
    assert d.detected
    assert d.command == "cd service && go test ./..."


def test_skip_dirs_are_ignored(tmp_path):
    # A vendored node_modules must not register as a second module.
    _write(tmp_path / "pyproject.toml", "[tool.uv]\n")
    _write(tmp_path / "tests/test_x.py", "def test_x():\n    assert True\n")
    _write(tmp_path / "node_modules/dep/package.json", json.dumps({"scripts": {"test": "x"}}))
    d = detect_test_command(tmp_path)
    assert d.detected
    assert d.command == "uv run pytest"


def test_nested_module_name_with_space_is_shell_quoted(tmp_path):
    # A valid directory name with a space must not produce a broken `cd my app`.
    (tmp_path / "my app").mkdir()
    _write(tmp_path / "my app/package.json", json.dumps({"scripts": {"test": "jest"}}))
    _write(tmp_path / "service/go.mod", "module x\n")
    d = detect_test_command(tmp_path)
    commands = {s.command for s in d.stacks}
    assert "cd 'my app' && npm test" in commands


def test_nested_module_name_with_metacharacters_is_quoted(tmp_path):
    (tmp_path / "a;rm -rf b").mkdir()
    _write(tmp_path / "a;rm -rf b/go.mod", "module x\n")
    d = detect_test_command(tmp_path)
    assert d.detected
    # The whole directory name is a single quoted argument to cd.
    assert d.command == "cd 'a;rm -rf b' && go test ./..."


def test_malformed_node_scripts_does_not_crash(tmp_path):
    # A non-object `scripts` value must fail closed (placeholder), not raise.
    _write(tmp_path / "package.json", json.dumps({"scripts": "not-an-object"}))
    d = detect_test_command(tmp_path)
    assert d.detected  # still a node project
    assert d.command == "npm test"
    assert "no tests found" in d.note.lower()  # no real test script detected


def test_nested_underscore_test_files_count_as_tests(tmp_path):
    # tests/unit/foo_test.py should register as "has tests" (guidance note).
    _write(tmp_path / "pyproject.toml", "[tool.uv]\n")
    _write(tmp_path / "tests/unit/foo_test.py", "def test_x():\n    assert True\n")
    d = detect_test_command(tmp_path)
    assert d.detected
    assert "no tests found" not in d.note.lower()


def test_is_placeholder_command_predicate():
    assert not is_placeholder_command("uv run pytest")
    assert not is_placeholder_command(None)
    assert not is_placeholder_command("")
