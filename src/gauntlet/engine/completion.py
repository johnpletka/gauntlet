"""Portable completion evidence; a local journal always takes precedence."""
from __future__ import annotations

from gauntlet.engine import gitops
from gauntlet.engine.execution import RunPaths
from gauntlet.engine.manifest import Manifest
from gauntlet.logging.redact import RedactingWriter

FILENAME = "completion.json"


def commit_completion(paths: RunPaths, manifest: Manifest, writer: RedactingWriter) -> str | None:
    """Commit only terminal evidence onto the run branch, idempotently.

    Do not export/track the live manifest here: merging that into an operator
    checkout would make a later dedicated-worktree rollback dirty the checkout.

    Never switches a checkout. Under ``dedicated`` the run worktree IS on the
    run branch and the export is a normal bookkeeping commit there. Under
    ``same_tree`` the operator's checkout may legitimately be on another
    branch when ``approve``/``reject`` complete the run (only ``resume``
    checks the run branch out first); checking the run branch out here would
    move the operator's tree — or refuse over their uncommitted edits, after
    the run is already persisted ``done``. So when the checkout is elsewhere
    the snapshot is committed straight onto the branch ref through the object
    database (:func:`gitops.commit_file_to_branch`), leaving HEAD, index and
    working files exactly as the operator had them.
    """
    work_root = paths.work_root
    target = paths.bookkeeping_root / FILENAME
    target.resolve().relative_to(work_root.resolve())  # fail closed on symlink escape
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump_json(indent=2) + "\n"
    # This is an export, never Manifest.write_atomic (which appends a journal).
    writer.write_text(target, payload)
    relpath = target.relative_to(work_root).as_posix()
    message = f"gauntlet: complete {manifest.slug}"
    if gitops.current_branch(work_root) == manifest.branch:
        return gitops.commit_run_bookkeeping(
            work_root, message, [relpath], identity=gitops.ENGINE_IDENTITY,
        )
    return gitops.commit_file_to_branch(
        work_root, manifest.branch, relpath, payload, message,
        identity=gitops.ENGINE_IDENTITY,
    )
