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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from gauntlet.engine.detect import TestCommandDetection, detect_test_command

SCAFFOLD_DIR = Path(__file__).resolve().parent.parent / "scaffold"


class InitError(RuntimeError):
    """`gauntlet init` cannot proceed without risking existing state (fail closed)."""

# The console script both CLIs ultimately invoke (the entry point from FR-1.1).
# Kept as the bare binary name: `gauntlet doctor` probes PATH for it, and this
# substring is the stable marker that recognises a judge-wired PreToolUse entry.
HOOK_COMMAND = "gauntlet-judge-hook"

# The string actually wired into a CLI's PreToolUse `command` hook. It is an
# install-tolerant POSIX launcher around HOOK_COMMAND, not the bare binary, so a
# teammate who has NOT installed Gauntlet sees ZERO hook errors: the bare command
# would exit 127 ("command not found") on every tool call, which the CLI surfaces
# as a (non-blocking) per-call hook-error notice. The launcher instead:
#   * when the hook IS installed: ``exec``s it, so the real hook's stdout and exit
#     code — including the exit-2 DENY and its own GAUNTLET_RUN_ID gating — pass
#     through unchanged (behaviour is byte-identical to the bare wiring); else
#   * inside an active gauntlet run (GAUNTLET_RUN_ID set) with the hook missing:
#     FAILS CLOSED (exit 2), so a broken install can never let a run proceed
#     silently ungated (CLAUDE.md §2 "fail closed"); else
#   * outside a run with the hook missing: stands aside silently (exit 0, no
#     output) — the zero-notice case for a non-installer.
# Shell form (no ``args``): Claude Code / Codex run this under POSIX sh on the
# project's supported platforms (macOS, Linux, WSL2 — native-Windows users follow
# the README's WSL2 path). ``command -v`` avoids a subshell; ``exec`` is what makes
# the deny exit code survive. Validated branch-by-branch in tests/unit/test_hook_launcher.py.
HOOK_WIRED_COMMAND = (
    'command -v gauntlet-judge-hook >/dev/null 2>&1 && exec gauntlet-judge-hook '
    '|| { if [ -n "${GAUNTLET_RUN_ID:-}" ]; then '
    'echo "gauntlet-judge-hook not on PATH during an active gauntlet run; '
    'failing closed" >&2; exit 2; fi; exit 0; }'
)

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
CUSTOMIZED = "customized"  # --from-repo: a committed aid the team has customized (present, not generated)


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


def _guard_regular_destination(repo_root: Path, rel: str) -> None:
    """Fail closed if a generated destination is unsafe to write (review F-001).

    ``Path.exists()``/``is_file()`` dereference symlinks, so a dangling
    ``<asset_root>/prd-stub.md`` symlink reads as *absent*, reaches
    ``shutil.copyfile``, and writes *through* the link — outside the repository;
    a symlink pointing at a regular file would likewise be accepted as valid
    state. Checking only the destination leaf is insufficient: a symlinked
    *parent* directory (e.g. ``.gauntlet`` → an external dir) leaves the leaf a
    non-symlink, yet ``mkdir``/``write_text``/``copyfile`` still follow the parent
    link and mutate paths outside the repo. We therefore:

    1. reject a symlink at any existing path component between ``repo_root`` and
       the destination via :meth:`Path.is_symlink` (which does not dereference) —
       ``repo_root`` itself is excluded, since a repo legitimately rooted on a
       symlinked path (e.g. a macOS worktree under ``/var``) is not our concern;
    2. reject a non-regular leaf (directory, FIFO, …) once symlinks are ruled out;
    3. verify the resolved destination remains contained within the resolved
       repository root (defense in depth).
    """
    target = repo_root / rel
    current = repo_root
    for part in Path(rel).parts[:-1]:  # parent components only; leaf handled below
        current = current / part
        if current.is_symlink():
            raise InitError(
                f"{rel} has a symlinked parent directory "
                f"({current.relative_to(repo_root).as_posix()}); refusing to "
                "write through it (it may resolve outside the repository). Move "
                "it aside and re-run `gauntlet init`."
            )
    if target.is_symlink():
        raise InitError(
            f"{rel} is a symlink; refusing to write through it (it may resolve "
            "outside the repository). Move it aside and re-run `gauntlet init`."
        )
    if target.exists() and not target.is_file():
        raise InitError(
            f"{rel} exists but is not a regular file; refusing to clobber "
            "unexpected state. Move it aside and re-run `gauntlet init`."
        )
    # With no symlinked component, ``target`` resolves inside the repo; assert it
    # so any path that still escapes (e.g. ``..`` traversal in a future caller)
    # fails closed rather than writing outside the tree.
    repo_resolved = repo_root.resolve()
    resolved = target.resolve()
    if resolved != repo_resolved and repo_resolved not in resolved.parents:
        raise InitError(
            f"{rel} resolves outside the repository ({resolved}); refusing to "
            "write. Move it aside and re-run `gauntlet init`."
        )


def _effective_asset_root(repo_root: Path, *, from_repo: bool) -> str:
    """The ``asset_root`` that will be in force when the skill/stub install.

    On an existing repo (or any ``--from-repo`` run) it is the configured value;
    on a fresh scaffold it is the ``asset_root`` of the config ``init`` is about
    to write (the scaffold default), since the skill/stub land *after* that write.
    Used by the preflight so it guards the destinations init will actually touch.
    """
    cfg = repo_root / ".gauntlet" / "config.yaml"
    if from_repo or cfg.exists():
        return _resolve_asset_root(repo_root)
    try:
        from gauntlet.engine.config import RunConfig

        return RunConfig.load(SCAFFOLD_DIR / "config.yaml").asset_root
    except Exception:
        return "."


def _preflight_destinations(repo_root: Path, *, from_repo: bool) -> None:
    """Reject every malformed generated destination BEFORE any write (review F-005).

    A non-regular or symlinked target must abort ``init`` *without* mutating the
    repo — otherwise a fresh repo with a pre-existing malformed stub destination
    gets numerous files written and only then raises. The skill and stub are
    checked in both modes (an adopter repo may already carry them); the asset
    files only on a fresh scaffold, since ``--from-repo`` never writes them.
    """
    from gauntlet.engine import prd_stub as PS
    from gauntlet.engine import skill as S

    asset_root = _effective_asset_root(repo_root, from_repo=from_repo)
    rels = [S.SKILL_REL, PS.stub_rel(asset_root)]
    if not from_repo:
        rels = [dst for _src, dst in _asset_pairs()] + rels
    for rel in rels:
        _guard_regular_destination(repo_root, rel)


def init_repo(repo_root: Path, *, from_repo: bool = False) -> InitResult:
    """Scaffold (or verify) the Gauntlet assets and wiring in ``repo_root``."""
    result = InitResult()

    # Fail closed on malformed pre-existing state BEFORE any write (review F-005):
    # a symlinked/non-regular destination aborts init without mutating the repo.
    _preflight_destinations(repo_root, from_repo=from_repo)

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
    # The structured stub template installs under the same asset_root (§4.3).
    _scaffold_stub(repo_root, result, from_repo=from_repo)
    _wire_claude_hook(repo_root, result)
    _wire_codex_hook(repo_root, result, from_repo=from_repo)
    _ensure_gitignore_guidance(repo_root, result)
    _warn_if_skill_ignored(repo_root, result)
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
    asset_root = _resolve_asset_root(repo_root)
    # Malformed pre-existing state (a symlink or non-regular node at the skill
    # path) is rejected in _preflight_destinations before any write (FR-3.2 /
    # review F-001, F-005), so the destination here is absent or a regular file.

    if from_repo:
        # The committed skill is authoritative; never write it. Report
        # present/missing/customized so `--from-repo`'s classification cannot
        # disagree with what a write-mode re-run would refresh (FR-3.1, review
        # F-004): the same version-keyed re-render-and-compare predicate decides.
        if not target.exists():
            result.add(rel, MISSING)
        elif S.classify_skill(target.read_text()) == "customization":
            result.add(rel, CUSTOMIZED, "committed skill is customized; left to the team")
        else:
            result.add(rel, PRESENT, "committed generated skill")
        return

    rendered = S.render_skill(S.current_template_path().read_text(), asset_root)

    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered)
        result.add(rel, CREATED, "PRD-authoring skill (thin pointer)")
        return

    existing = target.read_text()
    if S.classify_skill(existing) == "generated":
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


def _scaffold_stub(repo_root: Path, result: InitResult, *, from_repo: bool) -> None:
    """Install the structured PRD stub template to ``<asset_root>/prd-stub.md`` (§4.3).

    Posture mirrors the skill installer: create-if-absent, idempotent, never-clobber
    a customization, fail-closed via :class:`InitError` on malformed pre-existing
    state (rejected in :func:`_preflight_destinations` before any write — FR-3.2 /
    review F-005). Recognition of a prior-generated stub is the version-keyed
    compare in :func:`prd_stub.classify_stub` (review F-004), so an *unmodified*
    generated stub may be refreshed to the current template on a version bump (§4.5)
    while a customization is left byte-for-byte intact. ``--from-repo`` reports
    present/missing/customized via the same predicate (FR-3.1).
    """
    from gauntlet.engine import prd_stub as PS

    asset_root = _resolve_asset_root(repo_root)
    rel = PS.stub_rel(asset_root)
    target = repo_root / rel
    # Malformed pre-existing state (a symlink or non-regular node at the stub
    # path) is rejected in _preflight_destinations before any write (FR-3.2 /
    # review F-001, F-005), so the destination here is absent or a regular file.

    if from_repo:
        # The committed stub is authoritative; never write it — report
        # present/missing/customized via the same predicate a write-mode re-run
        # would use, so the two can never disagree (FR-3.1, review F-004).
        if not target.exists():
            result.add(rel, MISSING)
        elif PS.classify_stub(target.read_text()) == "customization":
            result.add(rel, CUSTOMIZED, "committed stub is customized; left to the team")
        else:
            result.add(rel, PRESENT, "committed generated stub")
        return

    current = PS.packaged_stub_path().read_text()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(current)
        result.add(rel, CREATED, "structured PRD stub template")
        return

    existing = target.read_text()
    if PS.classify_stub(existing) == "generated":
        # Unmodified generated stub: refresh it when the current template has moved
        # on (a version bump), and never otherwise (§4.5 — the only overwrite init
        # performs). The stub has no asset_root-dependent path, so unlike the skill
        # there is no stale-warning case for a customization — it is simply kept.
        if existing != current:
            target.write_text(current)
            result.add(rel, REFRESHED, "updated unmodified generated stub to current template")
        else:
            result.add(rel, SKIPPED, "generated stub up to date")
        return

    result.add(rel, SKIPPED, "customized; left unchanged")


def _hook_entry() -> dict:
    return {
        "matcher": "*",
        "hooks": [{"type": "command", "command": HOOK_WIRED_COMMAND, "timeout": 15}],
    }


def _iter_pretooluse_hooks(settings: dict):
    """Yield each ``(entry, hook)`` dict under PreToolUse, skipping odd shapes."""
    for entry in settings.get("hooks", {}).get("PreToolUse", []) or []:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook, dict):
                yield entry, hook


def _wire_claude_hook(repo_root: Path, result: InitResult) -> None:
    """Merge the PreToolUse → judge hook into .claude/settings.json (FR-7.3).

    Merge, not overwrite: any other settings the repo carries survive. The wired
    command is the install-tolerant launcher (see HOOK_WIRED_COMMAND) so a teammate
    who never installed Gauntlet sees no per-call hook-error notice. A re-run is
    idempotent; a legacy bare-command entry written by an older gauntlet is upgraded
    in place to the tolerant launcher — the only command rewrite init performs, and
    safe because such an entry is unambiguously gauntlet-generated.
    """
    rel = ".claude/settings.json"
    path = repo_root / rel
    if not path.exists():
        # Ship the scaffolded default verbatim — it carries the wired launcher plus
        # the explanatory _comment a teammate reads.
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SCAFFOLD_DIR / "claude-settings.json", path)
        result.add(rel, CREATED, "PreToolUse → install-tolerant judge launcher")
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

    # Recognise an existing gauntlet wiring; upgrade only an exact legacy bare
    # command (unambiguously generated) to the tolerant launcher. A hand-rolled
    # wrapping that still calls the hook is treated as a customization and left be.
    present = False
    upgraded = False
    for _entry, hook in _iter_pretooluse_hooks(settings):
        cmd = hook.get("command") or ""
        if cmd == HOOK_COMMAND:
            hook["command"] = HOOK_WIRED_COMMAND
            present = upgraded = True
        elif HOOK_COMMAND in cmd:
            present = True

    if present:
        if upgraded:
            path.write_text(json.dumps(settings, indent=2) + "\n")
            result.add(rel, WIRED, "upgraded judge hook to the install-tolerant launcher")
        else:
            result.add(rel, PRESENT, "PreToolUse already wired to the judge")
        return

    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("PreToolUse", []).append(_hook_entry())
    path.write_text(json.dumps(settings, indent=2) + "\n")
    result.add(rel, WIRED, "PreToolUse → install-tolerant judge launcher")


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


def _warn_if_skill_ignored(repo_root: Path, result: InitResult) -> None:
    """Warn (never edit) if a foreign ignore rule would exclude the skill (FR-1.4).

    ``init``'s own ``.gitignore`` guidance never excludes ``.claude/skills/``, but a
    repo ``.gitignore``, a parent-directory ``.gitignore``, ``.git/info/exclude``,
    or the global ``core.excludesFile`` might — and a silently-ignored skill never
    reaches a teammate's clone (defeating G4/FR-1.2). We ask git itself
    (``git check-ignore -v``, which consults *every* effective ignore source) and,
    when a rule matches, emit a WARNING naming the rule's source plus the
    remediation. We do **not** edit a maintainer's foreign rule — that is their
    call. When git is unavailable or this is not a work tree there is nothing to
    check (and the skill gates nothing), so we stay silent.
    """
    from gauntlet.engine import skill as S

    rel = S.SKILL_REL
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-v", rel],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    # git check-ignore: 0 = a rule matches (ignored); 1 = committable (the good
    # case); 128 = not a work tree / fatal. Only an actual match is worth warning.
    if proc.returncode != 0:
        return
    first = next((ln for ln in proc.stdout.splitlines() if ln.strip()), "")
    # `-v` format is "<source>:<linenum>:<pattern>\t<pathname>"; the source is the
    # ignore file the maintainer owns (or the global core.excludesFile path).
    source = first.split(":", 1)[0] if first else "an effective .gitignore rule"
    result.add(
        rel, WARNED,
        f"would be excluded by {source}; the committable skill must reach git — "
        "commit it with `git add -f` or amend that ignore rule (init left it intact)",
    )


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
