"""``gauntlet init`` — idempotent project scaffolding (FR-1.2, FR-4.5).

Scaffolds the committable Gauntlet assets into a repo so a teammate who clones
it gets the identical workflow:

* ``.gauntlet/config.yaml`` — agent profiles + identities (FR-2.1).
* ``pipelines/standard.yaml`` — the default 3-gate pipeline (FR-5.1).
* ``prompts/`` + ``schemas/`` — the versioned prompt templates and structured
  output schemas the pipeline references.
* ``policy.yaml`` — the judge fast-path rules (FR-7.6).
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
import shutil
from dataclasses import dataclass, field
from pathlib import Path

SCAFFOLD_DIR = Path(__file__).resolve().parent.parent / "scaffold"

# The hook command both CLIs invoke (the console script from FR-1.1).
HOOK_COMMAND = "gauntlet-judge-hook"

# Marker bounding the FR-4.5 guidance block in .gitignore, so re-runs detect it.
GITIGNORE_MARKER = "# --- Gauntlet (added by `gauntlet init`"

# scaffold-relative source -> repo-relative target for the committable assets.
# Directories are expanded file-by-file so a re-run can skip/keep per file.
_ASSET_FILES = {"config.yaml": ".gauntlet/config.yaml", "policy.yaml": "policy.yaml"}
_ASSET_DIRS = {
    "pipelines": "pipelines",
    "prompts": "prompts",
    "schemas": "schemas",
}

# Action verbs recorded per touched path.
CREATED = "created"
SKIPPED = "skipped"      # asset already present; left untouched (idempotent)
WIRED = "wired"          # hook wiring / gitignore guidance added
PRESENT = "present"      # wiring already in place; nothing to do
MISSING = "missing"      # --from-repo: a required committed asset is absent


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
        shutil.copyfile(src, target)
        result.add(dst_rel, CREATED)

    _wire_claude_hook(repo_root, result)
    _wire_codex_hook(repo_root, result, from_repo=from_repo)
    _ensure_gitignore_guidance(repo_root, result)
    return result


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

    try:
        settings = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}

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
