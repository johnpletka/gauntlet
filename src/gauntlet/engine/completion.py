"""Portable completion evidence; a local journal always takes precedence."""
from __future__ import annotations

from gauntlet.engine import gitops
from gauntlet.engine.execution import RunPaths
from gauntlet.engine.manifest import Manifest
from gauntlet.logging.redact import RedactingWriter

FILENAME = "completion.json"


def commit_completion(paths: RunPaths, manifest: Manifest, writer: RedactingWriter) -> str | None:
    """Commit only terminal evidence in the run's worktree, idempotently.

    Do not export/track the live manifest here: merging that into an operator
    checkout would make a later dedicated-worktree rollback dirty the checkout.
    """
    work_root = paths.work_root
    if gitops.current_branch(work_root) != manifest.branch:
        gitops.checkout_branch(work_root, manifest.branch)
    target = paths.bookkeeping_root / FILENAME
    target.resolve().relative_to(work_root.resolve())  # fail closed on symlink escape
    target.parent.mkdir(parents=True, exist_ok=True)
    # This is an export, never Manifest.write_atomic (which appends a journal).
    writer.write_text(target, manifest.model_dump_json(indent=2) + "\n")
    return gitops.commit_run_bookkeeping(
        work_root, f"gauntlet: complete {manifest.slug}",
        [target.relative_to(work_root).as_posix()], identity=gitops.ENGINE_IDENTITY,
    )
