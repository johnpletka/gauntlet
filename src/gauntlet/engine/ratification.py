"""First-class artifact ratification — `gauntlet resume --accept-artifacts` (#134).

An FR-10.4 escalation (``parked_for_response``) used to be resolvable only by
``resume --response "<prose>"``, which a cheap disposition model classifies
into the FR-3 enum before anything proceeds. Acceptance prose that happens to
carry imperative verbs ("proceed", "implement it as written") is routinely
classified ``amendment_required`` and re-parks — costing the operator a full
park round-trip for a decision that was never a prose question at all: *the
artifacts as they stand are the approved artifacts*.

This module owns the structured alternative. The operator's ``<run_root>/
<slug>/prd.md`` / ``plan.md`` are the governed-artifact AUTHORING SURFACE
(spike §14.2 option A; :mod:`~gauntlet.engine.govsync`), so a ratification is
a statement about *those bytes*: the engine hashes them, records the digests
on the manifest, and both disposition gates (:mod:`~gauntlet.engine.cycle`,
:mod:`~gauntlet.engine.steptypes`) short-circuit the pending response to
``proceed_in_place`` with zero model calls. The response's ``response_text``
is engine-generated from the digests — there is no prose to interpret, by
construction.

Manual governed-artifact edits are a SANCTIONED recovery workflow
(``execution.governed_artifact_paths``, R9): a ratification whose bytes differ
from the run's last-known approved digest is therefore never refused — it is
surfaced LOUDLY (a CLI audit line plus a durable manifest warning) and
recorded. Humans ratify; the engine audits.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from gauntlet.engine.govsync import GOVERNED_ARTIFACT_NAMES

# The fixed lead-in of every engine-generated ratification text. Rendered into
# `human-response.md` verbatim, so the builder/reviewer reads exactly what the
# manifest recorded.
ACCEPT_ARTIFACTS_TEXT_PREFIX = (
    "Operator ratified the current authoring-surface artifacts as approved:"
)


@dataclass(frozen=True)
class RatifiedDigest:
    """One governed artifact's digest at ratification time.

    ``prior_sha256`` is the run's last-known approved digest for the same
    artifact (a prior ratification, else the bytes committed on the run
    branch), or ``None`` when nothing is known. ``drifted`` is the audit
    predicate: the operator is ratifying bytes that differ from what the run
    last saw approved.
    """

    name: str
    sha256: str
    prior_sha256: str | None = None

    @property
    def drifted(self) -> bool:
        return self.prior_sha256 is not None and self.prior_sha256 != self.sha256


@dataclass
class ArtifactRatification:
    """The planned ratification: the digests and the engine-generated text."""

    digests: list[RatifiedDigest] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The `response_text` recorded on the manifest (engine-generated)."""
        parts = ", ".join(f"{d.name} sha256={d.sha256}" for d in self.digests)
        return f"{ACCEPT_ARTIFACTS_TEXT_PREFIX} {parts}"

    @property
    def drifted(self) -> list[RatifiedDigest]:
        return [d for d in self.digests if d.drifted]


def digest_bytes(data: bytes) -> str:
    """SHA-256 hex of ``data`` — the same form the governed records use."""
    return hashlib.sha256(data).hexdigest()


def plan_ratification(
    authoring_root: Path, *, known: dict[str, str] | None = None
) -> ArtifactRatification:
    """Hash the governed artifacts present under ``authoring_root``.

    ``authoring_root`` is the slug dir in the operator's checkout — the
    authoring surface, never the run worktree copy (which may lag or lead it;
    the ratification is about what the human sees). ``known`` maps artifact
    name → the last-known approved digest, for the drift audit. Raises
    ``ValueError`` when no governed artifact exists there: a ratification of
    nothing would record an empty approval and is refused (fail closed).
    """
    known = known or {}
    digests: list[RatifiedDigest] = []
    for name in GOVERNED_ARTIFACT_NAMES:
        path = authoring_root / name
        if not path.is_file():
            continue
        digests.append(
            RatifiedDigest(
                name=name,
                sha256=digest_bytes(path.read_bytes()),
                prior_sha256=known.get(name),
            )
        )
    if not digests:
        raise ValueError(
            "no governed artifact (prd.md / plan.md) exists under "
            f"{authoring_root} to ratify; --accept-artifacts records an "
            "approval of the artifacts as they stand and refuses to approve nothing"
        )
    return ArtifactRatification(digests=digests)


def drift_note(digest: RatifiedDigest, response_id: str) -> str:
    """The durable manifest warning for a ratification of drifted bytes."""
    return (
        f"artifact ratification {response_id}: {digest.name} ratified at "
        f"sha256={digest.sha256}, which differs from the run's last-known "
        f"approved digest sha256={digest.prior_sha256}. Recorded as approved "
        "as-is (manual governed-artifact edits are sanctioned in recovery; "
        "R9/FR-10.4) — audit the edit if it was not intended."
    )


def drift_audit_line(digest: RatifiedDigest) -> str:
    """The loud pre-drive CLI line for the same condition (no id yet)."""
    return (
        f"AUDIT: --accept-artifacts ratifies {digest.name} at "
        f"sha256={digest.sha256}, which DIFFERS from the run's last-known "
        f"approved digest sha256={digest.prior_sha256}; recording it as "
        "approved as-is (manual governed-artifact edits are sanctioned) — "
        "verify the edit was intended."
    )


def ratified_audit_line(digest: RatifiedDigest) -> str:
    """The per-artifact CLI line naming what is being ratified."""
    return f"ratifying {digest.name} sha256={digest.sha256} as approved"
