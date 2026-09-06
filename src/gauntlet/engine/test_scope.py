"""Engine-owned phase test context; test graph selection belongs to the adopter (#160)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

PREFIX = "GAUNTLET_TEST_"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=True, timeout=30,
    ).stdout


def prepare(root: Path, config, phase_start_sha: str | None, *, full: bool = False) -> dict:
    """Validate a persisted commit, inventory the actual tree, and choose a trusted command.

    No mutable step base, branch/ref expansion, or caller environment is accepted as the
    phase anchor. Any Git ambiguity falls back to full tests, never to an empty selection.
    """
    evidence = {
        "version": 1, "mode": "full", "reason": "full_requested" if full else "not_configured",
        "command": config.test_command, "base_sha": None, "head_sha": None,
        "head_tree": None, "changed_paths": [], "deleted_paths": [],
        "worktree_fingerprint": None,
    }
    affected = config.phase_test_command
    try:
        evidence["head_sha"] = _git(root, "rev-parse", "HEAD").strip()
        evidence["head_tree"] = _git(root, "rev-parse", "HEAD^{tree}").strip()
        if full or not affected:
            return evidence
        if not phase_start_sha or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", phase_start_sha):
            evidence["reason"] = "missing_or_invalid_phase_base"
            return evidence
        _git(root, "cat-file", "-e", f"{phase_start_sha}^{{commit}}")
        _git(root, "merge-base", "--is-ancestor", phase_start_sha, "HEAD")
        evidence["base_sha"] = phase_start_sha
        # --no-renames reports BOTH the deleted and added paths of a rename. Include
        # staged and unstaged changes even when one cancels the other, plus new files.
        paths = set()
        deleted = set()
        for args in [("diff", phase_start_sha), ("diff", "--cached", "HEAD"), ("diff",)]:
            paths.update(filter(None, _git(root, *args, "--name-only", "--no-renames", "-z", "--").split("\0")))
            deleted.update(filter(None, _git(root, *args, "--name-only", "--no-renames", "--diff-filter=D", "-z", "--").split("\0")))
        paths.update(filter(None, _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")))
        evidence["changed_paths"] = sorted(paths)
        evidence["deleted_paths"] = sorted(deleted)
        digest = hashlib.sha256(evidence["head_tree"].encode())
        for name in sorted(paths):
            p = root / name
            if not p.parent.resolve().is_relative_to(root.resolve()):
                evidence["reason"] = "changed_path_outside_worktree"
                return evidence
            digest.update(name.encode())
            if p.is_symlink():
                digest.update(b"link:" + os.fsencode(os.readlink(p)))
            elif p.is_file():
                digest.update(str(p.stat().st_mode).encode())
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif p.exists():
                # Submodules/directories need project-wide validation.
                evidence["reason"] = "unsupported_changed_entry"
                return evidence
            else:
                digest.update(b"missing")
        evidence["worktree_fingerprint"] = digest.hexdigest()
        if deleted:
            evidence["reason"] = "deleted_paths"
        elif not paths:
            evidence["reason"] = "empty_change_set"
        elif len(json.dumps(evidence).encode()) > 24_000:
            evidence["reason"] = "change_set_too_large"
        else:
            evidence.update(mode="phase", reason="validated_phase_base", command=affected)
    except (subprocess.SubprocessError, OSError, UnicodeError, RuntimeError):
        evidence.update(mode="full", reason="git_context_unavailable", command=config.test_command)
    return evidence


def environment(evidence: dict, base: dict[str, str]) -> dict[str, str]:
    """Replace ambient test hints; verifier callers pass their already-stripped env."""
    env = {k: v for k, v in base.items() if not k.startswith(PREFIX)}
    env[PREFIX + "MODE"] = evidence["mode"]
    if evidence["mode"] == "phase":
        env[PREFIX + "BASE_SHA"] = evidence["base_sha"]
        env[PREFIX + "CONTEXT"] = json.dumps(evidence, ensure_ascii=True)
    return env
