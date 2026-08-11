"""Durable publish baseline for governed artifacts (issue #97).

Under `dedicated`, the operator's checkout is the governed-artifact AUTHORING
surface (spike §14.2 option A) and every root resolution used to republish its
`prd.md`/`plan.md` bytes into the run worktree unconditionally. That contract
is one-directional and the run branch is not: mid-phase fix rounds legitimately
AMEND the governed artifact on the run branch (amendments-ledger entries,
FR-10.4 upstream fixes), after which the checkout copy LAGS the branch and the
next `resume` republished stale bytes over the ratified amendments — a
git-visible pure deletion that then failed the FR-9.3 clean-handoff guard.

The correction is a three-way compare, and it needs one piece of durable state
that did not exist before: **what the engine last published**. This module owns
that record. It is a small JSON file in the run-instance dir (``state_root``,
beside ``manifest.json``) rather than a Manifest field, deliberately: the
manifest is the P6 journal's regenerated projection (R8), so a projection field
would need its own journal event kind to survive regeneration — and the publish
baseline is engine-local sync bookkeeping, not run history anyone replays. The
file is written atomically (write-then-rename), matching how the manifest
itself is persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# The two artifacts the authoring-surface sync governs (§14.2 option A). The
# publish baseline is only meaningful for these; other step outputs flow one
# way and never re-enter the three-way compare.
GOVERNED_ARTIFACT_NAMES = ("prd.md", "plan.md")

# Lives beside manifest.json in the run-instance dir. Per-run on purpose: the
# baseline describes THIS run's publish history, and a new run starts from a
# fresh `start`-verb publish.
STATE_FILENAME = "governed-published.json"

# Where a checkout copy is preserved when a first-contact back-sync (a run
# predating this record) must replace bytes it cannot prove were never a real
# operator edit. Never silently destroy human-authored bytes.
BACKUP_DIRNAME = "governed-checkout-backup"


class GovernedArtifactDivergence(RuntimeError):
    """Three-way divergence: checkout, last-published and run branch all differ.

    Raised by :meth:`RunManager._sync_governed_artifacts` when the operator's
    authoring copy AND the run branch have both moved since the engine last
    published — publishing either side would silently overwrite the other, so
    the engine refuses loudly and mutates nothing. The message names all three
    states and both file paths, and tells the operator exactly how to resolve.
    """


def digest(data: bytes) -> str:
    """SHA-256 hex of ``data`` — the hash form every governed record uses."""
    return hashlib.sha256(data).hexdigest()


def load_published(state_root: Path) -> dict[str, dict]:
    """The recorded publish baselines, ``{}`` when absent.

    Each entry is ``{"published": <sha256 of the bytes the engine last
    published/adopted>, "branch": <sha256 of the run branch's committed bytes
    at that moment, or None when untracked>}``. Both are needed: "did the
    branch move?" must be answerable while an engine publish sits uncommitted
    in the tree, and "is this commit a move?" must answer NO when the branch
    merely committed the published bytes verbatim.

    Absent or unreadable is not an error: a run predating this record has no
    baseline, and the sync's first-contact rule handles that case explicitly
    rather than guessing here.
    """
    try:
        raw = json.loads((state_root / STATE_FILENAME).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(value.get("published"), str):
            branch = value.get("branch")
            out[str(key)] = {
                "published": value["published"],
                "branch": branch if isinstance(branch, str) else None,
            }
    return out


def branch_moved(record: dict, committed_sha256: str | None) -> bool:
    """Has the run branch's committed content moved since ``record`` was taken?

    Committing exactly the published bytes is NOT a move — that is the normal
    phase-commit of an engine publish, and calling it a move would turn the
    first operator edit after any phase commit into a false three-way
    divergence.
    """
    return committed_sha256 not in (record["branch"], record["published"])


def record_published(
    state_root: Path, name: str, *, published: str, branch: str | None
) -> None:
    """Durably advance the publish baseline for ``name``. Atomic; dedup'd.

    Called at every point the checkout and run-tree copies are made to agree —
    the root-resolution publish, a producer step's mid-drive
    ``publish_artifact``, and the gate-time ``adopt_artifact`` back-sync. Any
    writer that moves the bytes without moving this record makes the next
    three-way compare misread the agreement as a unilateral edit.
    """
    entry = {"published": published, "branch": branch}
    current = load_published(state_root)
    if current.get(name) == entry:
        return
    current[name] = entry
    state_root.mkdir(parents=True, exist_ok=True)
    tmp = state_root / (STATE_FILENAME + ".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, state_root / STATE_FILENAME)


def backup_checkout_copy(state_root: Path, name: str, data: bytes) -> Path:
    """Preserve checkout bytes a first-contact back-sync is about to replace.

    Only the no-baseline migration path needs this: with no record, the engine
    cannot prove the divergent checkout copy was never a real operator edit, so
    the bytes are kept recoverable (and a durable manifest warning names this
    path) instead of being silently overwritten.
    """
    backups = state_root / BACKUP_DIRNAME
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backups / f"{stamp}-{name}"
    path.write_bytes(data)
    return path
