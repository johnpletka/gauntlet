"""``gauntlet init`` — idempotent project scaffolding (FR-1.2, FR-4.5).

Scaffolds the committable Gauntlet assets into a repo so a teammate who clones
it gets the identical workflow:

* ``.gauntlet/config.yaml`` — agent profiles + identities (FR-2.1). Its
  ``test_command`` is detected from the repo's build markers rather than
  hard-coded (issue #18); a multi-module or unrecognised repo gets a
  fail-closed placeholder plus guidance instead of a wrong default.
* ``.gauntlet/pins.yaml`` — verified CLI versions ``gauntlet doctor`` checks
  installed versions against for drift (FR-1.5).
* ``.gauntlet/pipelines/standard.yaml`` — the default 3-gate pipeline (FR-5.1).
* ``.gauntlet/prompts/`` + ``.gauntlet/schemas/`` — the versioned prompt
  templates and structured output schemas the pipeline references.
* ``.gauntlet/policy.yaml`` — the judge fast-path rules (FR-7.6).
* hook wiring into ``.claude/settings.json`` and the repo-level ``.codex``
  hooks config (FR-7.3).
* ``.gitignore`` guidance (FR-4.5).

**Idempotent.** A plain re-run never clobbers an existing asset (the team may
have customized it) — it reports the file as skipped. Hook wiring is *merged*,
never overwritten, so other settings in ``.claude/settings.json`` survive.

``--from-repo`` is the team-adopter path: the repo already carries the
committed assets, so init skips asset templates entirely and only ensures the
machine-local hook wiring and ``.gitignore`` guidance (reporting any required
asset that is missing).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from gauntlet.engine.detect import TestCommandDetection, detect_test_command

SCAFFOLD_DIR = Path(__file__).resolve().parent.parent / "scaffold"


class InitError(RuntimeError):
    """`gauntlet init` cannot proceed without risking existing state (fail closed)."""

# The hook command both CLIs invoke (the console script from FR-1.1).
HOOK_COMMAND = "gauntlet-judge-hook"

# Marker bounding the FR-4.5 guidance block in .gitignore, so re-runs detect it.
GITIGNORE_MARKER = "# --- Gauntlet (added by `gauntlet init`"

# scaffold-relative source -> repo-relative target for the committable assets.
# Directories are expanded file-by-file so a re-run can skip/keep per file.
# The config target gets per-project test-command detection on create (issue
# #18), so it is written through ``_scaffold_config`` rather than copied verbatim.
CONFIG_TARGET = ".gauntlet/config.yaml"
_ASSET_FILES = {
    "config.yaml": CONFIG_TARGET,
    # The pin file doctor checks installed CLI versions against (FR-1.5); a
    # fresh repo cannot validate drift without it (review F-003).
    "pins.yaml": ".gauntlet/pins.yaml",
    "policy.yaml": ".gauntlet/policy.yaml",
}
# Adopter repos consolidate every gauntlet-owned asset under .gauntlet/ — the
# scaffolded config sets `asset_root: .gauntlet`, so the engine resolves them
# there. (Gauntlet's own repo keeps these at the root via the default
# `asset_root: "."`.) The scaffold source files carry bare, root-relative refs,
# so they stay byte-identical to the repo's canonical assets regardless.
_ASSET_DIRS = {
    "pipelines": ".gauntlet/pipelines",
    "prompts": ".gauntlet/prompts",
    "schemas": ".gauntlet/schemas",
}

# Action verbs recorded per touched path.
CREATED = "created"
SKIPPED = "skipped"      # asset already present; left untouched (idempotent)
WIRED = "wired"          # hook wiring / gitignore guidance added
PRESENT = "present"      # wiring already in place; nothing to do
MISSING = "missing"      # --from-repo: a required committed asset is absent
REFRESHED = "refreshed"  # an unmodified generated file updated to the current template (§4.5)
WARNED = "warned"        # advisory: a customization that looks stale; left untouched


@dataclass(frozen=True)
class InitAction:
    path: str       # repo-relative path
    action: str     # one of the verbs above
    detail: str = ""


@dataclass
class InitResult:
    actions: list[InitAction] = field(default_factory=list)

    def add(self, path: str, action: str, detail: str = "") -> None:
        self.actions.append(InitAction(path=path, action=action, detail=detail))

    @property
    def missing(self) -> list[InitAction]:
        return [a for a in self.actions if a.action == MISSING]


def _asset_pairs() -> list[tuple[Path, str]]:
    """(absolute scaffold source, repo-relative target) for every asset file."""
    pairs: list[tuple[Path, str]] = []
    for src_rel, dst_rel in _ASSET_FILES.items():
        pairs.append((SCAFFOLD_DIR / src_rel, dst_rel))
    for src_dir, dst_dir in _ASSET_DIRS.items():
        base = SCAFFOLD_DIR / src_dir
        for src in sorted(base.rglob("*")):
            if src.is_file():
                rel = src.relative_to(base)
                pairs.append((src, f"{dst_dir}/{rel.as_posix()}"))
    return pairs


def init_repo(repo_root: Path, *, from_repo: bool = False) -> InitResult:
    """Scaffold (or verify) the Gauntlet assets and wiring in ``repo_root``."""
    result = InitResult()

    for src, dst_rel in _asset_pairs():
        target = repo_root / dst_rel
        if from_repo:
            # The team's committed asset is authoritative; never write it.
            result.add(dst_rel, PRESENT if target.exists() else MISSING)
            continue
        if target.exists():
            result.add(dst_rel, SKIPPED, "exists; left unchanged")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if dst_rel == CONFIG_TARGET:
            _scaffold_config(src, target, repo_root, result, dst_rel)
            continue
        shutil.copyfile(src, target)
        result.add(dst_rel, CREATED)

    # The skill resolves its playbook reference under the repo's asset_root, so
    # install it after the config (which carries asset_root) has been written.
    _scaffold_skill(repo_root, result, from_repo=from_repo)
    _wire_claude_hook(repo_root, result)
    _wire_codex_hook(repo_root, result, from_repo=from_repo)
    _ensure_gitignore_guidance(repo_root, result)
    return result


# Matches the scaffold's single ``test_command:`` line (with optional inline
# comment) so we can swap in the per-project detection (issue #18).
_TEST_COMMAND_RE = re.compile(r"^test_command:.*$", re.MULTILINE)


def _render_test_command_block(detection: TestCommandDetection) -> str:
    """The ``test_command:`` line (plus guidance comment when not auto-detected)."""
    command = detection.command.replace('"', '\\"')
    line = f'test_command: "{command}"'
    if detection.detected:
        return line
    # Ambiguous / unknown: precede the fail-closed placeholder with the reason so
    # the operator knows exactly what to set, right where they will edit it.
    lines = ["# gauntlet init could not determine a single test command for this repo:"]
    lines += [f"#   {part}" for part in _wrap(detection.note)]
    if detection.stacks:
        # Each candidate on its own line so it stays copy-pasteable (not wrapped).
        lines.append("# detected module commands:")
        lines += [f"#   {s.module}: {s.command}" for s in detection.stacks]
    lines.append("# The placeholder below fails the test gate on purpose — replace it.")
    lines.append(line)
    return "\n".join(lines)


def _wrap(text: str, width: int = 74) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width) or [text]


def _scaffold_config(
    src: Path, target: Path, repo_root: Path, result: InitResult, dst_rel: str
) -> None:
    """Write ``.gauntlet/config.yaml`` with a project-appropriate test_command.

    The scaffold ships a Python/pytest default; on a fresh repo we detect the
    actual stack and substitute it, or drop in a fail-closed placeholder when the
    stack is multi-module or unrecognised (issue #18). The rest of the file is
    untouched, so it stays byte-aligned with the canonical scaffold.
    """
    detection = detect_test_command(repo_root)
    text = src.read_text()
    block = _render_test_command_block(detection)
    new_text, n = _TEST_COMMAND_RE.subn(lambda _m: block, text, count=1)
    if n == 0:
        # The scaffold should always carry a test_command line; if it ever does
        # not, fail closed rather than silently shipping a config with none.
        raise InitError(
            "scaffold config.yaml has no `test_command:` line to substitute; "
            "the bundled scaffold is malformed"
        )
    target.write_text(new_text)
    result.add(dst_rel, CREATED, detection.note)


def _resolve_asset_root(repo_root: Path) -> str:
    """The repo's configured ``asset_root`` (default ``"."``).

    The skill's playbook reference is rendered under this root (FR-1.3). On a
    fresh ``init`` the scaffolded ``.gauntlet/config.yaml`` already carries the
    adopter default (``.gauntlet``); Gauntlet's own repo pins ``"."``. An absent
    or unreadable config falls back to ``"."`` — the engine's own default — so a
    transient config fault never aborts skill install (the skill gates nothing).
    """
    cfg = repo_root / ".gauntlet" / "config.yaml"
    if cfg.exists():
        try:
            from gauntlet.engine.config import RunConfig

            return RunConfig.load(cfg).asset_root
        except Exception:
            pass
    return "."


def _scaffold_skill(repo_root: Path, result: InitResult, *, from_repo: bool) -> None:
    """Install the committable ``gauntlet-prd-author`` skill (FR-1.1, §4.5).

    Posture mirrors the judge-hook wiring: create-if-absent, idempotent,
    never-clobber a customization, fail-closed on malformed pre-existing state.
    Recognition of a prior-generated file is the version-keyed
    re-render-and-compare in :mod:`gauntlet.engine.skill` (review F-004), so an
    *unmodified* generated file may be refreshed to the current template/path
    (§4.5) while a customization is only ever warned about, never modified.
    """
    from gauntlet.engine import skill as S

    rel = S.SKILL_REL
    target = repo_root / rel
    # Fail closed on malformed pre-existing state (FR-3.2): a non-regular node at
    # the skill path must never be silently clobbered, mirroring _wire_claude_hook.
    if target.exists() and not target.is_file():
        raise InitError(
            f"{rel} exists but is not a regular file; refusing to clobber "
            "unexpected state. Move it aside and re-run `gauntlet init`."
        )

    if from_repo:
        # The committed skill is authoritative; never write it (full
        # present/missing/customized classification is P3).
        result.add(rel, PRESENT if target.exists() else MISSING)
        return

    asset_root = _resolve_asset_root(repo_root)
    rendered = S.render_skill(S.current_template_path().read_text(), asset_root)

    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered)
        result.add(rel, CREATED, "PRD-authoring skill (thin pointer)")
        return

    existing = target.read_text()
    if S.classify_skill(existing, asset_root) == "generated":
        # Unmodified generated file: refresh it when the current template or the
        # resolved playbook path has moved on (§4.5 — the only overwrite init does).
        if existing != rendered:
            target.write_text(rendered)
            result.add(rel, REFRESHED, "updated unmodified generated skill to current template")
        else:
            result.add(rel, SKIPPED, "generated skill up to date")
        return

    # A customization: never overwrite. Warn (only) when it looks stale (§4.5).
    if S.skill_looks_stale(existing, asset_root):
        result.add(
            rel, WARNED,
            f"customized skill looks stale: playbook ref {S.playbook_ref(asset_root)!r} "
            "not found; review it (re-run does not modify a customization)",
        )
    else:
        result.add(rel, SKIPPED, "customized; left unchanged")


def _hook_entry() -> dict:
    return {
        "matcher": "*",
        "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 15}],
    }


def _pretooluse_has_hook(settings: dict) -> bool:
    """True if a PreToolUse entry already routes to the gauntlet hook command."""
    for entry in settings.get("hooks", {}).get("PreToolUse", []) or []:
        for hook in entry.get("hooks", []) or []:
            if hook.get("command") == HOOK_COMMAND:
                return True
    return False


def _wire_claude_hook(repo_root: Path, result: InitResult) -> None:
    """Merge the PreToolUse → judge hook into .claude/settings.json (FR-7.3).

    Merge, not overwrite: any other settings the repo carries survive, and a
    re-run is a no-op once the gauntlet hook is present.
    """
    rel = ".claude/settings.json"
    path = repo_root / rel
    if not path.exists():
        # Ship the scaffolded default verbatim — it carries the wired hook plus
        # the explanatory _comment a teammate reads.
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SCAFFOLD_DIR / "claude-settings.json", path)
        result.add(rel, CREATED, "PreToolUse → gauntlet-judge-hook")
        return

    # Fail closed on malformed external state (CLAUDE.md §2): a parse error or a
    # non-object document must NOT be silently replaced — that would destroy a
    # user's existing settings during an "idempotent" re-run (review F-007).
    raw = path.read_text()
    try:
        settings = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise InitError(
            f"{rel} is not valid JSON ({exc}); refusing to overwrite it. Fix the "
            "file (or move it aside) and re-run `gauntlet init` to merge the "
            "PreToolUse judge hook."
        ) from exc
    if not isinstance(settings, dict):
        raise InitError(
            f"{rel} is not a JSON object (found {type(settings).__name__}); "
            "refusing to overwrite it. Fix the file (or move it aside) and re-run "
            "`gauntlet init`."
        )

    if _pretooluse_has_hook(settings):
        result.add(rel, PRESENT, "PreToolUse already wired to the judge")
        return

    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("PreToolUse", []).append(_hook_entry())
    path.write_text(json.dumps(settings, indent=2) + "\n")
    result.add(rel, WIRED, "PreToolUse → gauntlet-judge-hook")


def _wire_codex_hook(repo_root: Path, result: InitResult, *, from_repo: bool) -> None:
    """Write the repo-level Codex hooks config (FR-1.2).

    Inert on the pinned codex build (it does not fire exec hooks — see the
    file's own comment / BOOTSTRAP-NOTES #10), but committable and
    forward-looking. Written verbatim from the scaffold when absent; never
    clobbered on re-run.
    """
    rel = ".codex/hooks.json"
    path = repo_root / rel
    if path.exists():
        result.add(rel, PRESENT, "codex hooks config present")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SCAFFOLD_DIR / "codex-hooks.json", path)
    result.add(rel, WIRED if from_repo else CREATED, "inert on pinned codex; forward-looking")


def _ensure_gitignore_guidance(repo_root: Path, result: InitResult) -> None:
    """Append the FR-4.5 .gitignore guidance once (idempotent)."""
    rel = ".gitignore"
    path = repo_root / rel
    guidance = (SCAFFOLD_DIR / "gitignore-guidance.txt").read_text()
    existing = path.read_text() if path.exists() else ""
    if GITIGNORE_MARKER in existing:
        result.add(rel, PRESENT, "gauntlet guidance already present")
        return
    prefix = existing
    if existing and not existing.endswith("\n"):
        prefix += "\n"
    path.write_text(prefix + guidance)
    result.add(rel, WIRED if existing else CREATED, "FR-4.5 guidance")
