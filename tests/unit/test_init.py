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

from gauntlet.engine import skill as S
from gauntlet.engine.config import RunConfig
from gauntlet.engine.init import (
    CREATED,
    MISSING,
    PRESENT,
    REFRESHED,
    SCAFFOLD_DIR,
    SKIPPED,
    WARNED,
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
    for schema in (
        "findings.json", "triage.json", "confirm.json", "resume-disposition.json",
    ):
        checks[SCAFFOLD_DIR / "schemas" / schema] = REPO / "schemas" / schema
    for prompt in (SCAFFOLD_DIR / "prompts").glob("*"):
        checks[prompt] = REPO / "prompts" / prompt.name
    for bundled, canonical in checks.items():
        assert bundled.read_bytes() == canonical.read_bytes(), f"drift: {bundled.name}"


# ---- PRD-authoring skill install (P1: FR-1.1, FR-1.3, FR-3.2, §4.5) ---------

def test_init_creates_skill_with_provenance_and_adopter_playbook_path(tmp_path):
    result = init_repo(tmp_path)
    skill_file = tmp_path / S.SKILL_REL
    assert skill_file.exists()
    actions = {a.path: a.action for a in result.actions}
    assert actions[S.SKILL_REL] == CREATED
    text = skill_file.read_text()
    # A fresh adopter init scaffolds asset_root .gauntlet, so the skill points at
    # the adopter-relative playbook path — repo-relative, never absolute (FR-1.3).
    assert "`.gauntlet/prompts/prd-author.md`" in text
    assert S.PLAYBOOK_PLACEHOLDER not in text
    assert S.validate_skill_frontmatter(text) == []
    assert S.classify_skill(text, ".gauntlet") == "generated"


def test_init_skill_install_is_idempotent(tmp_path):
    init_repo(tmp_path)
    before = (tmp_path / S.SKILL_REL).read_text()
    result = init_repo(tmp_path)
    actions = {a.path: a.action for a in result.actions}
    assert actions[S.SKILL_REL] == SKIPPED
    assert (tmp_path / S.SKILL_REL).read_text() == before


def test_init_does_not_clobber_customized_skill(tmp_path):
    init_repo(tmp_path)
    skill_file = tmp_path / S.SKILL_REL
    custom = skill_file.read_text() + "\n<!-- hand-tuned by the maintainer -->\n"
    skill_file.write_text(custom)
    result = init_repo(tmp_path)
    assert skill_file.read_text() == custom  # byte-for-byte intact
    actions = {a.path: a.action for a in result.actions}
    assert actions[S.SKILL_REL] in (SKIPPED, WARNED)


def test_init_refreshes_unmodified_generated_skill(tmp_path, monkeypatch):
    # §4.5: an unmodified generated file is refreshed when the current template
    # moves on (a version bump), and never otherwise. Simulate a v2 template while
    # resolving the installed file's v1 to its original template so it is still
    # recognized as generated (not a customization).
    init_repo(tmp_path)
    skill_file = tmp_path / S.SKILL_REL
    original_tmpl = S.current_template_path().read_text()

    new_tmpl = tmp_path / "new_template.md"
    new_tmpl.write_text(original_tmpl + "\n<!-- template v2 line -->\n")
    orig_tmpl = tmp_path / "orig_template.md"
    orig_tmpl.write_text(original_tmpl)
    monkeypatch.setattr(S, "current_template_path", lambda: new_tmpl)
    monkeypatch.setattr(S, "version_template_path", lambda v: orig_tmpl if v == 1 else None)

    result = init_repo(tmp_path)
    actions = {a.path: a.action for a in result.actions}
    assert actions[S.SKILL_REL] == REFRESHED
    assert "template v2 line" in skill_file.read_text()


def test_init_warns_on_stale_provenance_bearing_skill(tmp_path, monkeypatch):
    # A generated skill whose asset_root later changed no longer matches the
    # re-render (fail safe → customization), but it carries provenance and a
    # drifted playbook path → init WARNS (naming the drift) and never modifies it.
    init_repo(tmp_path)
    skill_file = tmp_path / S.SKILL_REL
    before = skill_file.read_text()  # references .gauntlet/prompts/prd-author.md
    # Flip the repo's asset_root to "." so the rendered ref would now differ.
    cfg = tmp_path / ".gauntlet/config.yaml"
    cfg.write_text(cfg.read_text().replace("asset_root: .gauntlet", 'asset_root: "."'))
    result = init_repo(tmp_path)
    actions = {a.path: a.action for a in result.actions}
    assert actions[S.SKILL_REL] == WARNED
    assert skill_file.read_text() == before  # never modified


def test_init_fails_closed_on_malformed_skill_state(tmp_path):
    # FR-3.2: a non-regular node where the skill file belongs is malformed
    # pre-existing state — refuse rather than clobber, mirroring the settings guard.
    skill_path = tmp_path / S.SKILL_REL
    skill_path.mkdir(parents=True)  # a directory where the SKILL.md should be
    with pytest.raises(InitError):
        init_repo(tmp_path)


def test_from_repo_reports_skill_present_or_missing(tmp_path):
    # --from-repo never writes the skill; it reports present/missing (full
    # customized classification is P3).
    missing = init_repo(tmp_path, from_repo=True)
    assert {a.path: a.action for a in missing.actions}[S.SKILL_REL] == MISSING
    init_repo(tmp_path)  # now scaffold it
    present = init_repo(tmp_path, from_repo=True)
    assert {a.path: a.action for a in present.actions}[S.SKILL_REL] == PRESENT
