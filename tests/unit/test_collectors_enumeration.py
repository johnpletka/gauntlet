"""#129 — the pytest enumeration argv derived from a compound ``test_command``.

The ``tests`` shell step runs ``test_command`` through a shell, where ``&&`` is
control flow. The acceptance gate's collector executes a derived argv WITHOUT a
shell, so connectors and non-pytest segments must never survive into it.
"""
from __future__ import annotations

from gauntlet.engine.collectors import _pytest_enumeration_command


def test_a_compound_command_is_reduced_to_its_pytest_segment() -> None:
    command = _pytest_enumeration_command(
        "uv run pytest tests -q && uv run ruff check packages && uv run mypy"
    )
    assert "&&" not in command
    assert "ruff" not in command and "mypy" not in command
    assert command == (
        "uv", "run", "pytest", "tests",
        "--collect-only", "-q", "-p", "no:cacheprovider",
    )


def test_a_single_command_is_unchanged_by_the_segment_cut() -> None:
    assert _pytest_enumeration_command("uv run pytest tests -q") == (
        "uv", "run", "pytest", "tests",
        "--collect-only", "-q", "-p", "no:cacheprovider",
    )


def test_the_pytest_segment_is_found_even_when_it_is_not_first() -> None:
    command = _pytest_enumeration_command(
        "uv run ruff check packages && uv run pytest tests -q"
    )
    assert command == (
        "uv", "run", "pytest", "tests",
        "--collect-only", "-q", "-p", "no:cacheprovider",
    )


def test_semicolon_and_pipe_connectors_are_cut_too() -> None:
    command = _pytest_enumeration_command("pytest tests -q ; echo done | cat")
    assert command == (
        "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider",
    )
