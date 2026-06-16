"""`gauntlet init` — idempotent scaffolding + hook wiring (P6, FR-1.2/4.5).

Asserts the file set init produces, that the scaffolded config + pipeline are
valid against the P3 loader (plan P6 test strategy), idempotency, the
`--from-repo` path, and that the shipped scaffold assets do not drift from the
repo's canonical ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.engine.config import RunConfig
from gauntlet.engine.init import (
    CREATED,
    MISSING,
    PRESENT,
    SCAFFOLD_DIR,
    SKIPPED,
    WIRED,
    InitError,
    init_repo,
)
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.validate import validate_pipeline
from gauntlet.pins import load_pins

REPO = Path(__file__).resolve().parents[2]

EXPECTED_ASSETS = {
    ".gauntlet/config.yaml",
    ".gauntlet/pins.yaml",
    ".gauntlet/policy.yaml",
    ".gauntlet/pipelines/standard.yaml",
    ".gauntlet/schemas/findings.json",
    ".gauntlet/schemas/triage.json",
    ".gauntlet/schemas/confirm.json",
    ".gauntlet/prompts/review-document.md",
    ".gauntlet/prompts/review-code.md",
    ".gauntlet/prompts/plan-author.md",
    ".gauntlet/prompts/implement-phase.md",
    ".gauntlet/prompts/commit-message.md",
    ".gauntlet/prompts/cycle-review.md",
    ".gauntlet/prompts/cycle-rereview.md",
    ".gauntlet/prompts/cycle-fix.md",
    ".gauntlet/prompts/cycle-confirm.md",
    ".gauntlet/prompts/triage.md",
}


def test_init_fresh_repo_creates_full_asset_set(tmp_path):
    result = init_repo(tmp_path)
    created = {a.path for a in result.actions if a.action == CREATED}
    # every expected asset created
    assert EXPECTED_ASSETS <= created
    # wiring touched the two hook files + .gitignore
    assert (tmp_path / ".claude/settings.json").exists()
    assert (tmp_path / ".codex/hooks.json").exists()
    assert (tmp_path / ".gitignore").exists()
    for rel in EXPECTED_ASSETS:
        assert (tmp_path / rel).exists(), rel


def test_scaffolded_config_and_pipeline_validate(tmp_path):
    init_repo(tmp_path)
    config = RunConfig.load(tmp_path / ".gauntlet/config.yaml")
    pipeline, phash = load_pipeline(tmp_path / ".gauntlet/pipelines/standard.yaml")
    report = validate_pipeline(pipeline, config)
    assert report.ok()
    assert phash.startswith("sha256:")
    # the cycle's default prompt set + referenced schema all landed
    assert (tmp_path / ".gauntlet/schemas/findings.json").exists()


def test_init_scaffolds_pin_file(tmp_path):
    # doctor needs a pin file to validate CLI version drift (FR-1.5, review F-003).
    init_repo(tmp_path)
    pins_path = tmp_path / ".gauntlet/pins.yaml"
    assert pins_path.exists()
    pins = load_pins(pins_path)
    assert "claude" in pins.clis and "codex" in pins.clis


def test_init_refuses_to_clobber_malformed_claude_settings(tmp_path):
    # Fail closed on malformed external state; never silently destroy the user's
    # existing settings.json during an "idempotent" re-run (review F-007).
    claude = tmp_path / ".claude"
    claude.mkdir()
    original = "{ this is not valid JSON, do not eat it"
    (claude / "settings.json").write_text(original)
    with pytest.raises(InitError):
        init_repo(tmp_path)
    assert (claude / "settings.json").read_text() == original


def test_init_refuses_non_object_claude_settings(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    original = '["a list, not a settings object"]'
    (claude / "settings.json").write_text(original)
    with pytest.raises(InitError):
        init_repo(tmp_path)
    assert (claude / "settings.json").read_text() == original


def test_claude_hook_wired_with_judge_command(tmp_path):
    init_repo(tmp_path)
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    commands = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert "gauntlet-judge-hook" in commands


def test_init_is_idempotent(tmp_path):
    init_repo(tmp_path)
    before = {
        p: (tmp_path / p).read_text()
        for p in EXPECTED_ASSETS | {".claude/settings.json", ".codex/hooks.json", ".gitignore"}
    }
    second = init_repo(tmp_path)
    # nothing re-created; everything reported skipped/present
    assert all(a.action in (SKIPPED, PRESENT) for a in second.actions), [
        a for a in second.actions if a.action not in (SKIPPED, PRESENT)
    ]
    after = {p: (tmp_path / p).read_text() for p in before}
    assert after == before  # byte-for-byte unchanged


def test_init_does_not_clobber_customized_asset(tmp_path):
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet/config.yaml").write_text("# my custom config\nbase_branch: develop\n")
    result = init_repo(tmp_path)
    assert (tmp_path / ".gauntlet/config.yaml").read_text().startswith("# my custom config")
    actions = {a.path: a.action for a in result.actions}
    assert actions[".gauntlet/config.yaml"] == SKIPPED


def test_init_merges_into_existing_claude_settings(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {}}))
    init_repo(tmp_path)
    settings = json.loads((claude / "settings.json").read_text())
    assert settings["model"] == "opus"  # preserved
    commands = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert commands.count("gauntlet-judge-hook") == 1
    # a second run must not duplicate the hook
    init_repo(tmp_path)
    settings = json.loads((claude / "settings.json").read_text())
    commands = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert commands.count("gauntlet-judge-hook") == 1


def test_gitignore_guidance_appended_once(tmp_path):
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    init_repo(tmp_path)
    text = (tmp_path / ".gitignore").read_text()
    assert "__pycache__/" in text  # preserved
    assert ".gauntlet/runs/*/active-run.txt" in text
    assert text.count("--- Gauntlet (added by") == 1
    init_repo(tmp_path)
    assert (tmp_path / ".gitignore").read_text().count("--- Gauntlet (added by") == 1


def test_from_repo_skips_asset_templates_but_wires_hooks(tmp_path):
    # A configured repo that already carries the committed assets.
    result = init_repo(tmp_path, from_repo=True)
    actions = {a.path: a.action for a in result.actions}
    # assets are not scaffolded; they are reported missing here (none committed)
    assert actions[".gauntlet/config.yaml"] == MISSING
    assert actions[".gauntlet/policy.yaml"] == MISSING
    assert result.missing  # surfaced for the operator
    # but the machine-local wiring is still ensured
    assert (tmp_path / ".claude/settings.json").exists()
    assert (tmp_path / ".codex/hooks.json").exists()


def test_from_repo_reports_present_when_assets_exist(tmp_path):
    init_repo(tmp_path)            # first scaffold everything
    result = init_repo(tmp_path, from_repo=True)
    actions = {a.path: a.action for a in result.actions}
    assert actions[".gauntlet/config.yaml"] == PRESENT
    assert not result.missing


def test_init_detects_test_command_for_python_repo(tmp_path):
    # issue #18: a Python/uv repo gets `uv run pytest` written into the config.
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_x.py").write_text("def test_x():\n    assert True\n")
    init_repo(tmp_path)
    config = RunConfig.load(tmp_path / ".gauntlet/config.yaml")
    assert config.test_command == "uv run pytest"


def test_init_detects_node_test_command(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    init_repo(tmp_path)
    config = RunConfig.load(tmp_path / ".gauntlet/config.yaml")
    assert config.test_command == "npm test"


def test_init_writes_placeholder_for_unrecognised_repo(tmp_path):
    # An empty repo cannot be auto-detected; init writes a fail-closed placeholder
    # (not a wrong default) plus guidance, and the config still loads.
    from gauntlet.engine.detect import is_placeholder_command

    result = init_repo(tmp_path)
    config = RunConfig.load(tmp_path / ".gauntlet/config.yaml")
    assert is_placeholder_command(config.test_command)
    text = (tmp_path / ".gauntlet/config.yaml").read_text()
    assert "could not determine a single test command" in text
    detail = {a.path: a.detail for a in result.actions}[".gauntlet/config.yaml"]
    assert "no recognised build markers" in detail


def test_init_flags_multi_module_repo(tmp_path):
    # issue #18 concern #2: backend + frontend has no single command.
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/pyproject.toml").write_text("[tool.uv]\n")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/package.json").write_text('{"scripts": {"test": "jest"}}')
    from gauntlet.engine.detect import is_placeholder_command

    init_repo(tmp_path)
    config = RunConfig.load(tmp_path / ".gauntlet/config.yaml")
    assert is_placeholder_command(config.test_command)
    text = (tmp_path / ".gauntlet/config.yaml").read_text()
    assert "cd backend && uv run pytest" in text
    assert "cd frontend && npm test" in text


def test_shipped_scaffold_matches_repo_canonical_assets():
    """The bundled defaults must not drift from the repo's live assets.

    `config.yaml` is intentionally a clean default (the repo's is bootstrap-
    pinned), so it is excluded; everything else ships verbatim.
    """
    checks = {
        SCAFFOLD_DIR / "policy.yaml": REPO / "policy.yaml",
        SCAFFOLD_DIR / "pins.yaml": REPO / ".gauntlet/pins.yaml",
        SCAFFOLD_DIR / "pipelines/standard.yaml": REPO / "pipelines/standard.yaml",
        SCAFFOLD_DIR / "claude-settings.json": REPO / ".claude/settings.json",
    }
    for schema in ("findings.json", "triage.json", "confirm.json"):
        checks[SCAFFOLD_DIR / "schemas" / schema] = REPO / "schemas" / schema
    for prompt in (SCAFFOLD_DIR / "prompts").glob("*"):
        checks[prompt] = REPO / "prompts" / prompt.name
    for bundled, canonical in checks.items():
        assert bundled.read_bytes() == canonical.read_bytes(), f"drift: {bundled.name}"
