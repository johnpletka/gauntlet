"""`gauntlet init` — idempotent scaffolding + hook wiring (P6, FR-1.2/4.5).

Asserts the file set init produces, that the scaffolded config + pipeline are
valid against the P3 loader (plan P6 test strategy), idempotency, the
`--from-repo` path, and that the shipped scaffold assets do not drift from the
repo's canonical ones.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import prd_stub as PS
from gauntlet.engine import skill as S
from gauntlet.engine.config import RunConfig
from gauntlet.engine.init import (
    CREATED,
    CUSTOMIZED,
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
    from gauntlet.engine.init import HOOK_WIRED_COMMAND

    init_repo(tmp_path)
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    commands = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    # The install-tolerant launcher is wired (it *calls* the console script), so a
    # teammate without Gauntlet installed gets no per-call hook-error notice.
    assert HOOK_WIRED_COMMAND in commands
    assert all("gauntlet-judge-hook" in c for c in commands)


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
    assert len(commands) == 1 and "gauntlet-judge-hook" in commands[0]
    # a second run must not duplicate the hook
    init_repo(tmp_path)
    settings = json.loads((claude / "settings.json").read_text())
    commands = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert len(commands) == 1 and "gauntlet-judge-hook" in commands[0]


def test_init_upgrades_legacy_bare_hook_to_tolerant_launcher(tmp_path):
    # A repo wired by an older gauntlet (the bare `gauntlet-judge-hook` command) is
    # upgraded in place to the install-tolerant launcher on re-run, so existing
    # adopters stop emitting per-call hook errors for teammates without gauntlet.
    from gauntlet.engine.init import HOOK_COMMAND, HOOK_WIRED_COMMAND

    claude = tmp_path / ".claude"
    claude.mkdir()
    legacy = {
        "model": "opus",
        "hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [
                {"type": "command", "command": HOOK_COMMAND, "timeout": 15}]}]},
    }
    (claude / "settings.json").write_text(json.dumps(legacy))

    actions = {a.path: a.action for a in init_repo(tmp_path).actions}
    assert actions[".claude/settings.json"] == WIRED  # upgraded, not left untouched

    settings = json.loads((claude / "settings.json").read_text())
    assert settings["model"] == "opus"  # unrelated settings preserved
    hooks = settings["hooks"]["PreToolUse"]
    assert len(hooks) == 1 and len(hooks[0]["hooks"]) == 1  # no duplicate appended
    assert hooks[0]["hooks"][0]["command"] == HOOK_WIRED_COMMAND

    # Re-running is idempotent: the tolerant launcher is recognized as present.
    second = {a.path: a.action for a in init_repo(tmp_path).actions}
    assert second[".claude/settings.json"] == PRESENT
    again = json.loads((claude / "settings.json").read_text())
    assert again["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == HOOK_WIRED_COMMAND


def test_init_leaves_customized_hook_wrapping_untouched(tmp_path):
    # A hand-rolled wrapping that still calls the hook is a customization: init
    # recognizes it as already wired (no duplicate) and never rewrites it. Only an
    # exact legacy bare command is auto-upgraded.
    from gauntlet.engine.init import HOOK_WIRED_COMMAND

    claude = tmp_path / ".claude"
    claude.mkdir()
    custom_cmd = "nice -n 5 gauntlet-judge-hook"
    (claude / "settings.json").write_text(json.dumps(
        {"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [
                {"type": "command", "command": custom_cmd, "timeout": 20}]}]}}))

    actions = {a.path: a.action for a in init_repo(tmp_path).actions}
    assert actions[".claude/settings.json"] == PRESENT

    cmd = json.loads((claude / "settings.json").read_text())[
        "hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd == custom_cmd  # untouched: not upgraded, not duplicated
    assert cmd != HOOK_WIRED_COMMAND


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


def test_init_scaffolds_review_pipeline_and_issue_tracker_config(tmp_path):
    """FR-10.2: a fresh init ships pipelines/review.yaml + the review prompt and a
    commented issue_tracker block in the config."""
    from gauntlet.engine.pipeline import load_pipeline

    init_repo(tmp_path)
    review_yaml = tmp_path / ".gauntlet/pipelines/review.yaml"
    assert review_yaml.exists(), "init must ship pipelines/review.yaml (FR-10.2)"
    pipeline, _ = load_pipeline(review_yaml)  # loads + validates
    assert pipeline.name == "review"
    assert (tmp_path / ".gauntlet/prompts/review-code-intent.md").exists()

    config_text = (tmp_path / ".gauntlet/config.yaml").read_text()
    # A COMMENTED example (not an active block) carrying the Linear provider +
    # the env-var NAME (never a token).
    assert "# issue_tracker:" in config_text
    assert "provider: linear" in config_text
    assert "LINEAR_API_KEY" in config_text
    # It stays commented — RunConfig loads with tracker resolution disabled.
    assert RunConfig.load(tmp_path / ".gauntlet/config.yaml").issue_tracker is None


def test_shipped_scaffold_matches_repo_canonical_assets():
    """The bundled defaults must not drift from the repo's live assets.

    `config.yaml` is intentionally a clean default (the repo's is bootstrap-
    pinned), so it is excluded; everything else ships verbatim.
    """
    checks = {
        SCAFFOLD_DIR / "policy.yaml": REPO / "policy.yaml",
        SCAFFOLD_DIR / "pins.yaml": REPO / ".gauntlet/pins.yaml",
        SCAFFOLD_DIR / "pipelines/standard.yaml": REPO / "pipelines/standard.yaml",
        SCAFFOLD_DIR / "pipelines/review.yaml": REPO / "pipelines/review.yaml",
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
    assert S.classify_skill(text) == "generated"


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


def test_init_refreshes_unmodified_generated_skill_on_asset_root_change(tmp_path):
    # F-001: an *unmodified* generated skill whose resolved playbook path moved
    # because the repo's asset_root changed is still a generated file and MUST be
    # refreshed to the current configuration — not frozen as a customization and
    # left stale. (Previously this case was misclassified and only WARNED.)
    init_repo(tmp_path)
    skill_file = tmp_path / S.SKILL_REL
    before = skill_file.read_text()  # references .gauntlet/prompts/prd-author.md
    assert "`.gauntlet/prompts/prd-author.md`" in before
    # Flip the repo's asset_root to "." so the rendered ref now differs.
    cfg = tmp_path / ".gauntlet/config.yaml"
    cfg.write_text(cfg.read_text().replace("asset_root: .gauntlet", 'asset_root: "."'))
    result = init_repo(tmp_path)
    actions = {a.path: a.action for a in result.actions}
    assert actions[S.SKILL_REL] == REFRESHED
    after = skill_file.read_text()
    assert "`prompts/prd-author.md`" in after
    assert "`.gauntlet/prompts/prd-author.md`" not in after


def test_init_warns_on_stale_provenance_bearing_customization(tmp_path):
    # A *customized* (edited-body) skill that still carries provenance and whose
    # playbook ref drifted after an asset_root change → init WARNS (naming the
    # drift) and never modifies it. This is the genuine stale case; an unmodified
    # generated file is refreshed instead (see the test above, F-001).
    init_repo(tmp_path)
    skill_file = tmp_path / S.SKILL_REL
    # Edit the body so the file is a customization (no longer a clean rendering),
    # while keeping the provenance frontmatter and the .gauntlet playbook ref.
    custom = skill_file.read_text() + "\n<!-- hand-tuned by the maintainer -->\n"
    skill_file.write_text(custom)
    # Flip asset_root to "." so the customization's playbook ref is now stale.
    cfg = tmp_path / ".gauntlet/config.yaml"
    cfg.write_text(cfg.read_text().replace("asset_root: .gauntlet", 'asset_root: "."'))
    result = init_repo(tmp_path)
    actions = {a.path: a.action for a in result.actions}
    assert actions[S.SKILL_REL] == WARNED
    assert skill_file.read_text() == custom  # never modified


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


# ---- structured PRD stub template install (P2: FR-2.1 §4.3, FR-3.2) ---------

def _stub_rel(tmp_path: Path) -> str:
    from gauntlet.engine import prd_stub as PS

    return PS.stub_rel(RunConfig.load(tmp_path / ".gauntlet/config.yaml").asset_root)


def test_init_creates_stub_template_under_asset_root(tmp_path):
    from gauntlet.engine import prd_stub as PS

    result = init_repo(tmp_path)
    rel = _stub_rel(tmp_path)
    assert rel == ".gauntlet/prd-stub.md"  # fresh init scaffolds asset_root .gauntlet
    assert (tmp_path / rel).exists()
    assert {a.path: a.action for a in result.actions}[rel] == CREATED
    # the installed template is a valid gate input against the playbook manifest
    text = (tmp_path / rel).read_text()
    manifest = PS.parse_manifest((SCAFFOLD_DIR / "prompts" / "prd-author.md").read_text())
    PS.validate_template(text, manifest)


def test_init_stub_install_is_idempotent(tmp_path):
    init_repo(tmp_path)
    rel = _stub_rel(tmp_path)
    before = (tmp_path / rel).read_text()
    result = init_repo(tmp_path)
    assert {a.path: a.action for a in result.actions}[rel] == SKIPPED
    assert (tmp_path / rel).read_text() == before  # untouched


def test_init_fails_closed_on_malformed_stub_state(tmp_path):
    # FR-3.2 / review F-005: a non-regular node where the stub template belongs
    # is malformed pre-existing state — refuse rather than clobber, and mutate
    # nothing, mirroring the skill-path guard.
    init_repo(tmp_path)  # writes config so asset_root resolves to .gauntlet
    rel = _stub_rel(tmp_path)
    stub_path = tmp_path / rel
    stub_path.unlink()
    stub_path.mkdir()  # a directory where the stub file should be
    with pytest.raises(InitError):
        init_repo(tmp_path)
    assert stub_path.is_dir()  # left intact, not clobbered


def _tree(root: Path) -> set:
    """Every path under ``root`` (relative), for asserting init wrote nothing."""
    return {p.relative_to(root) for p in root.rglob("*")}


def test_malformed_stub_on_fresh_repo_aborts_without_mutation(tmp_path):
    # review F-005: on an OTHERWISE FRESH repo, a pre-existing non-regular stub
    # destination must abort init in preflight — before any asset/skill/config is
    # written. The earlier test inits successfully first, so it cannot catch a
    # partial fresh-init write; this one asserts the whole tree is unchanged.
    # A fresh init writes config asset_root .gauntlet, so the stub lands there.
    stub_dir = tmp_path / ".gauntlet" / "prd-stub.md"
    stub_dir.mkdir(parents=True)  # a directory where the stub file should be
    before = _tree(tmp_path)
    with pytest.raises(InitError):
        init_repo(tmp_path)
    assert _tree(tmp_path) == before  # nothing created, nothing clobbered


def test_init_refuses_dangling_symlink_stub_and_does_not_write_through_it(tmp_path):
    # review F-001: exists()/is_file() follow symlinks, so a DANGLING symlink at
    # the stub destination would read as absent and reach shutil.copyfile, which
    # writes THROUGH the link — outside the repo. Reject the symlink; never write.
    escape = tmp_path.parent / f"escape-{tmp_path.name}.md"
    assert not escape.exists()
    stub_link = tmp_path / ".gauntlet" / "prd-stub.md"
    stub_link.parent.mkdir(parents=True)
    stub_link.symlink_to(escape)  # dangling: target does not exist
    before = _tree(tmp_path)
    with pytest.raises(InitError, match="symlink"):
        init_repo(tmp_path)
    assert not escape.exists()        # the link target was never written through
    assert _tree(tmp_path) == before  # and the repo itself is untouched


def test_init_refuses_symlink_stub_pointing_at_a_regular_file(tmp_path):
    # review F-001: a symlink to an existing regular file must NOT be accepted as
    # valid state — every symlink at a generated destination is refused.
    real = tmp_path / "real-stub.md"
    real.write_text("not the gauntlet stub\n")
    stub_link = tmp_path / ".gauntlet" / "prd-stub.md"
    stub_link.parent.mkdir(parents=True)
    stub_link.symlink_to(real)
    with pytest.raises(InitError, match="symlink"):
        init_repo(tmp_path)
    assert real.read_text() == "not the gauntlet stub\n"  # untouched


def test_init_refuses_symlinked_parent_directory_and_does_not_write_through_it(tmp_path):
    # review F-001: the leaf guard alone is not enough — if a PARENT directory
    # (e.g. .gauntlet) is a symlink to an external dir, the leaf targets are not
    # themselves symlinks, so they pass and mkdir/write_text/copyfile follow the
    # parent link, mutating paths outside the repo. Reject the symlinked parent.
    external = tmp_path.parent / f"external-{tmp_path.name}"
    external.mkdir()
    before_external = {p.relative_to(external) for p in external.rglob("*")}
    gauntlet_link = tmp_path / ".gauntlet"
    gauntlet_link.symlink_to(external, target_is_directory=True)
    before = _tree(tmp_path)
    with pytest.raises(InitError, match="symlink"):
        init_repo(tmp_path)
    # nothing was written through the parent link, and the repo is untouched
    assert {p.relative_to(external) for p in external.rglob("*")} == before_external
    assert _tree(tmp_path) == before


def test_from_repo_reports_stub_present_or_missing(tmp_path):
    # --from-repo never writes the stub; it reports present/missing (full
    # customized classification is P3).
    missing = init_repo(tmp_path, from_repo=True)
    # asset_root defaults to "." when no config exists yet → prd-stub.md at root
    assert {a.path: a.action for a in missing.actions}["prd-stub.md"] == MISSING
    init_repo(tmp_path)  # now scaffold it (writes config: asset_root .gauntlet)
    rel = _stub_rel(tmp_path)
    present = init_repo(tmp_path, from_repo=True)
    assert {a.path: a.action for a in present.actions}[rel] == PRESENT


# ---- P3: three-mode propagation of BOTH aids (FR-3.1, §4.5) -----------------

def test_init_refreshes_unmodified_generated_stub(tmp_path, monkeypatch):
    # §4.5: an unmodified generated stub is refreshed when the current template
    # moves on (a version bump), and never otherwise — the stub analogue of the
    # skill refresh. Simulate a v2 template while resolving the installed v1 stub
    # to its original template so it is still recognized as generated.
    init_repo(tmp_path)
    rel = _stub_rel(tmp_path)
    stub_file = tmp_path / rel
    original = PS.packaged_stub_path().read_text()

    new_tmpl = tmp_path / "new_stub.md"
    new_tmpl.write_text(original + "\n<!-- stub template v2 line -->\n")
    orig_tmpl = tmp_path / "orig_stub.md"
    orig_tmpl.write_text(original)
    monkeypatch.setattr(PS, "packaged_stub_path", lambda: new_tmpl)
    monkeypatch.setattr(PS, "stub_version_template_path", lambda v: orig_tmpl if v == 1 else None)

    result = init_repo(tmp_path)
    actions = {a.path: a.action for a in result.actions}
    assert actions[rel] == REFRESHED
    assert "stub template v2 line" in stub_file.read_text()


def test_init_does_not_clobber_customized_stub(tmp_path):
    # A customized stub (provenance present but body edited → no byte match) is
    # never overwritten on re-run; it is reported skipped (never-clobber, FR-3.1).
    init_repo(tmp_path)
    rel = _stub_rel(tmp_path)
    stub_file = tmp_path / rel
    custom = stub_file.read_text() + "\n<!-- hand-tuned by the maintainer -->\n"
    stub_file.write_text(custom)
    result = init_repo(tmp_path)
    assert stub_file.read_text() == custom  # byte-for-byte intact
    assert {a.path: a.action for a in result.actions}[rel] == SKIPPED


def test_from_repo_reports_customized_for_both_aids(tmp_path):
    # FR-3.1: --from-repo reports present/missing/customized for BOTH aids via the
    # same predicate a write-mode re-run uses, so the report cannot disagree with
    # what a re-run would refresh.
    init_repo(tmp_path)
    rel = _stub_rel(tmp_path)
    skill_file = tmp_path / S.SKILL_REL
    skill_file.write_text(skill_file.read_text() + "\n<!-- custom -->\n")
    stub_file = tmp_path / rel
    stub_file.write_text(stub_file.read_text() + "\n<!-- custom -->\n")
    result = init_repo(tmp_path, from_repo=True)
    actions = {a.path: a.action for a in result.actions}
    assert actions[S.SKILL_REL] == CUSTOMIZED
    assert actions[rel] == CUSTOMIZED
    # a customized committed aid is never written, even in --from-repo mode
    assert skill_file.read_text().endswith("<!-- custom -->\n")


def test_combined_rerun_fail_closed_on_malformed_skill_without_mutating_stub(tmp_path):
    # review F-005: a malformed pre-existing state at EITHER generated path during a
    # both-aids re-run still raises InitError without mutation — the per-aid guards
    # (skill in P1, stub in P2) are not bypassed when both are installed together.
    init_repo(tmp_path)  # both aids present
    rel = _stub_rel(tmp_path)
    before_stub = (tmp_path / rel).read_text()
    skill_file = tmp_path / S.SKILL_REL
    skill_file.unlink()
    skill_file.mkdir()  # a non-regular node where the SKILL.md belongs
    with pytest.raises(InitError):
        init_repo(tmp_path)
    assert (tmp_path / rel).read_text() == before_stub  # stub not mutated


def test_second_repo_adopter_lands_both_aids_at_adopter_paths(tmp_path):
    # A distinct adopter repo (fresh init → asset_root .gauntlet) gets both aids at
    # the adopter paths, the skill carrying the adopter-relative playbook reference.
    init_repo(tmp_path)
    assert RunConfig.load(tmp_path / ".gauntlet/config.yaml").asset_root == ".gauntlet"
    assert (tmp_path / S.SKILL_REL).exists()
    assert (tmp_path / ".gauntlet/prd-stub.md").exists()
    assert "`.gauntlet/prompts/prd-author.md`" in (tmp_path / S.SKILL_REL).read_text()


# ---- P3: clone-to-different-path portability (FR-1.3 acceptance (b)) ---------

def test_committed_skill_reference_survives_relocation(tmp_path):
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    init_repo(repo_a)
    asset_root = RunConfig.load(repo_a / ".gauntlet/config.yaml").asset_root
    skill_text = (repo_a / S.SKILL_REL).read_text()
    ref = S.playbook_ref(asset_root)  # repository-relative, never absolute
    assert f"`{ref}`" in skill_text
    for absolute in ("/Users/", "/home/", "/private/", "/tmp/", str(repo_a)):
        assert absolute not in skill_text, absolute

    # Relocate the whole repo to a different absolute path: the committed skill's
    # repo-relative reference must still resolve to the playbook there (proves no
    # embedded source-machine path).
    repo_b = tmp_path / "b"
    shutil.copytree(repo_a, repo_b)
    assert (repo_b / ref).is_file()
    assert (repo_b / S.SKILL_REL).read_text() == skill_text  # byte-identical


# ---- P3: .gitignore committability + foreign-ignore-rule warning (FR-1.4) ----

def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, check=True)


def test_skill_committable_no_warning_when_not_ignored(tmp_path):
    # init's own .gitignore guidance does not exclude .claude/skills/, so a fresh
    # git repo leaves the skill committable: no foreign-ignore warning is emitted.
    _git_init(tmp_path)
    result = init_repo(tmp_path)
    warned = [a for a in result.actions if a.path == S.SKILL_REL and a.action == WARNED]
    assert warned == []
    # and git agrees the installed skill is not ignored
    check = subprocess.run(
        ["git", "check-ignore", S.SKILL_REL], cwd=tmp_path, capture_output=True, text=True
    )
    assert check.returncode == 1  # no rule matches → committable


def test_skill_committable_warns_on_foreign_ignore_rule_without_editing_it(tmp_path):
    # FR-1.4: a pre-existing info/exclude rule that matches .claude/skills/ makes
    # init WARN (naming the source) and proceed — it never edits the foreign rule.
    _git_init(tmp_path)
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.write_text(".claude/skills/\n")
    result = init_repo(tmp_path)
    warned = [a for a in result.actions if a.path == S.SKILL_REL and a.action == WARNED]
    assert warned, "expected a foreign-ignore warning for the skill"
    assert "exclude" in warned[0].detail.lower()  # names the ignoring source
    assert exclude.read_text() == ".claude/skills/\n"  # foreign rule left intact
