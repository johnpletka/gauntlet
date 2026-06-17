"""Test-command detection for ``gauntlet init`` (issue #18).

``gauntlet init`` used to hard-code ``test_command: "uv run pytest"`` into every
scaffolded config, which is only correct for a single-module Python/pytest repo
that already has tests. This module inspects on-disk build markers and proposes
the right command — or, when it cannot determine one unambiguously, returns a
**fail-closed placeholder** so a run halts with a clear message instead of
silently running the wrong test command (PRD §2: fail closed, data over
inference).

The detection is deliberately boring and deterministic: a fixed set of marker
files maps to a fixed command, scanned at the repo root and one directory level
down (enough to notice the backend/frontend split that motivated the issue),
with no network access and no heavy parsing.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

# A run whose test_command is this sentinel halts the test gate immediately with
# a message pointing the operator at the config. The marker substring lets
# ``gauntlet doctor`` recognise an un-configured command and WARN before a run.
PLACEHOLDER_MARKER = "gauntlet init could not auto-detect"
PLACEHOLDER_COMMAND = (
    "echo 'gauntlet: set test_command in .gauntlet/config.yaml — "
    f"{PLACEHOLDER_MARKER} a test command for this project' >&2 && exit 1"
)

# Directories we never descend into when looking for a second module.
_SKIP_DIRS = {
    ".git", ".gauntlet", ".github", ".venv", "venv", "node_modules",
    "__pycache__", "dist", "build", "target", "vendor", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages",
}


@dataclass(frozen=True)
class DetectedStack:
    """One self-contained module discovered in the repo."""

    module: str        # repo-relative dir ("." for the repo root)
    language: str      # python | node | rust | go
    command: str       # the test command to run that module (cd-prefixed if nested)
    has_tests: bool    # whether tests appear to exist yet (affects guidance, not the command)


@dataclass(frozen=True)
class TestCommandDetection:
    command: str                       # always set; the placeholder when not confidently detected
    status: str                        # detected | ambiguous | none
    stacks: tuple[DetectedStack, ...]
    note: str                          # human-readable summary / guidance

    @property
    def detected(self) -> bool:
        return self.status == "detected"


def is_placeholder_command(command: str | None) -> bool:
    """True if ``command`` is the un-configured fail-closed placeholder."""
    return bool(command) and PLACEHOLDER_MARKER in command


def _python_has_tests(d: Path) -> bool:
    if (d / "conftest.py").exists():
        return True
    for sub in ("tests", "test"):
        td = d / sub
        if td.is_dir() and (
            any(td.glob("test_*.py")) or any(td.glob("*_test.py"))
            or any(td.rglob("test_*.py")) or any(td.rglob("*_test.py"))
        ):
            return True
    return any(d.glob("test_*.py")) or any(d.glob("*_test.py"))


def _pyproject_uses_uv(d: Path) -> bool:
    if (d / "uv.lock").exists():
        return True
    pp = d / "pyproject.toml"
    if pp.exists():
        try:
            return "[tool.uv]" in pp.read_text()
        except OSError:
            return False
    return False


def _node_has_test_script(d: Path) -> bool:
    """True if package.json defines a real ``test`` script (not the npm-init stub)."""
    pkg = d / "package.json"
    if not pkg.exists():
        return False
    try:
        import json

        data = json.loads(pkg.read_text() or "{}")
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    # `scripts` may be malformed (null/string/list) in a hand-edited package.json;
    # treat anything that is not a mapping as "no test script" rather than crashing
    # detection (fail closed — the repo just gets the placeholder).
    scripts = data.get("scripts")
    script = scripts.get("test") if isinstance(scripts, dict) else None
    return isinstance(script, str) and bool(script.strip()) and "no test specified" not in script


def _node_package_manager(d: Path) -> str:
    if (d / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (d / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _detect_in_dir(d: Path) -> DetectedStack | None:
    """Identify the stack rooted at directory ``d`` (or None). First match wins."""
    rel = "." if d.name == "" else d.name  # placeholder; caller sets the real rel

    # Python
    if (
        (d / "pyproject.toml").exists()
        or (d / "setup.py").exists()
        or (d / "setup.cfg").exists()
        or (d / "pytest.ini").exists()
        or (d / "tox.ini").exists()
        or (d / "conftest.py").exists()
        or any(d.glob("requirements*.txt"))
    ):
        cmd = "uv run pytest" if _pyproject_uses_uv(d) else "pytest"
        return DetectedStack(rel, "python", cmd, _python_has_tests(d))

    # Node / JS
    if (d / "package.json").exists():
        cmd = f"{_node_package_manager(d)} test"
        return DetectedStack(rel, "node", cmd, _node_has_test_script(d))

    # Rust (cargo test exits 0 with no tests, so has_tests is not meaningful)
    if (d / "Cargo.toml").exists():
        return DetectedStack(rel, "rust", "cargo test", True)

    # Go (go test ./... exits 0 with no tests)
    if (d / "go.mod").exists():
        return DetectedStack(rel, "go", "go test ./...", True)

    return None


def _scan(repo_root: Path) -> list[DetectedStack]:
    """Detect a stack at the repo root and in each immediate subdirectory."""
    stacks: list[DetectedStack] = []
    root = _detect_in_dir(repo_root)
    if root is not None:
        stacks.append(DetectedStack(".", root.language, root.command, root.has_tests))

    for child in sorted(p for p in repo_root.iterdir() if p.is_dir()):
        if child.name in _SKIP_DIRS or child.name.startswith("."):
            continue
        found = _detect_in_dir(child)
        if found is None:
            continue
        # A nested module's command must cd into it (the gate runs at repo root).
        # Quote the dir name: it is interpolated into a shell command that later
        # runs with shell=True, so a space or metacharacter ("my app", "a;b")
        # would otherwise produce a broken or unsafe command.
        command = f"cd {shlex.quote(child.name)} && {found.command}"
        stacks.append(DetectedStack(child.name, found.language, command, found.has_tests))
    return stacks


def detect_test_command(repo_root: Path) -> TestCommandDetection:
    """Propose a ``test_command`` for ``repo_root``.

    Returns ``status``:

    * ``detected``  — exactly one module found; ``command`` is its test command.
    * ``ambiguous`` — more than one module (e.g. backend + frontend); ``command``
      is the fail-closed placeholder and ``note`` lists the candidates.
    * ``none``      — no recognised stack; ``command`` is the placeholder.
    """
    stacks = tuple(_scan(repo_root))

    if not stacks:
        return TestCommandDetection(
            command=PLACEHOLDER_COMMAND,
            status="none",
            stacks=stacks,
            note=(
                "no recognised build markers (pyproject.toml, package.json, "
                "Cargo.toml, go.mod, …) — set test_command to the command that "
                "runs this project's tests"
            ),
        )

    if len(stacks) > 1:
        return TestCommandDetection(
            command=PLACEHOLDER_COMMAND,
            status="ambiguous",
            stacks=stacks,
            note=(
                "multiple modules detected "
                f"({', '.join(s.module for s in stacks)}) — gauntlet runs one test "
                "command per phase; combine them or pick one"
            ),
        )

    only = stacks[0]
    note = f"detected {only.language} project → `{only.command}`"
    if not only.has_tests:
        note += " (no tests found yet; the test gate will fail until you add them)"
    return TestCommandDetection(
        command=only.command,
        status="detected",
        stacks=stacks,
        note=note,
    )
