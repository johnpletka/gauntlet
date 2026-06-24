"""Run manifest: the state-machine checkpoint (§7, FR-8.2, review F-003).

The manifest is written **write-ahead** — before and after every step — and
atomically (write-temp + ``os.replace``), so a ``kill -9`` at any instant
leaves either the prior or the next consistent state on disk, never a torn
file. The side-effect transaction boundary (review F-003) lives here too: each
worktree-touching step records its **base SHA** before running, so resume can
tell a clean re-entry from a step that died mid-edit.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- park reasons (FR-2.1) ---------------------------------------------------
# Single enum-valued discriminator that tells an UPSTREAM CONFLICT park from
# every other park (human_gate, budget/timeout halt, generic agent re-run). It
# is the ONLY signal `gauntlet resume --response` uses to decide whether a
# parked step is a conflict park; it carries no conflict content (the rich
# conflict metadata is deferred, PRD §7). v1 has exactly one value.
PARKED_REASON_UPSTREAM_CONFLICT = "upstream_conflict"

# --- human-response lifecycle states (FR-2, FR-7.1) --------------------------
# A `--response` entry is born ``pending`` (appended before the agent launches)
# and flips to ``consumed`` once the resumed agent reaches a terminal outcome
# (proceeds, re-parks, or fails). The ``state`` field is the single source of
# truth for idempotent crash recovery: a recovered ``pending`` entry is
# re-launched, never re-appended; a ``consumed`` entry is never re-executed.
RESPONSE_PENDING = "pending"
RESPONSE_CONSUMED = "consumed"

# --- step lifecycle states ---------------------------------------------------
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
INTERRUPTED = "interrupted"  # killed mid-step, dirty worktree (F-003)
PARKED = "parked"  # human_gate / interrupted-park awaiting a human
HALTED = "halted"  # budget/timeout guard tripped (FR-3.3)
SKIPPED = "skipped"  # `when:` false

# --- run lifecycle states ----------------------------------------------------
RUN_RUNNING = "running"
RUN_PARKED = "parked"
RUN_DONE = "done"
RUN_ABORTED = "aborted"
RUN_FAILED = "failed"


class PipelineRef(BaseModel):
    name: str
    version: int
    hash: str


class UsageTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None  # None until at least one priced call (§12 Q3)

    def add(self, usage: Any | None) -> None:
        if usage is None:
            return
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cached_input_tokens += usage.cached_input_tokens or 0
        if usage.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + usage.cost_usd


class HumanResponse(BaseModel):
    """One `gauntlet resume --response` decision, appended for the audit trail.

    Append-only (FR-2): once written, an entry's ``response_id`` never changes —
    it is how the builder references a response (FR-5/FR-10) and how crash
    recovery deduplicates (FR-7.1). ``state`` drives that idempotent recovery:
    ``pending`` immediately after append (before the agent launches), flipped to
    ``consumed`` once the resumed agent reaches a terminal outcome. The append /
    consume wiring lands in P3; P1 only defines the durable shape.
    """

    response_id: str
    response_text: str
    timestamp: str
    user: str
    response_attempt: int
    state: Literal["pending", "consumed"]


class StepRecord(BaseModel):
    id: str
    type: str
    status: str = PENDING
    agent: str | None = None
    session_id: str | None = None
    started: str | None = None
    ended: str | None = None
    attempts: int = 0
    # Transaction boundary (F-003): HEAD before the step touched the worktree.
    base_sha: str | None = None
    # Conflict-park discriminator (FR-2.1). CURRENT-STATE, not a latch: the
    # orchestrator clears it (back to None) on every non-conflict finalization of
    # the step and re-sets PARKED_REASON_UPSTREAM_CONFLICT only when that
    # execution halted on an UPSTREAM CONFLICT. A stale value therefore can never
    # cause a later generic park to be misclassified as a conflict park.
    parked_reason: str | None = None
    # Append-only audit trail of human `--response` decisions on this step
    # (FR-2). Recording/consume wiring is P3; P1 carries the schema only.
    human_responses: list[HumanResponse] = Field(default_factory=list)
    usage: UsageTotals = Field(default_factory=UsageTotals)
    notes: str | None = None
    # foreach binding key, when this record is one iteration of a fan-out step
    iteration: str | None = None
    # Step-emitted structured outcome counts for `gauntlet report --trend`
    # (FR-6.6): an adversarial_cycle records rounds, finding/verdict/confirm
    # tallies here so trend math reads the manifest, never the log dirs (the
    # plan's P7 test strategy is "trend-metric math from fixture manifests").
    metrics: dict[str, Any] = Field(default_factory=dict)


class CommitRecord(BaseModel):
    step_id: str
    phase: str  # the PN[.x] prefix
    sha: str


class Manifest(BaseModel):
    """The persisted run state (§7)."""

    run_id: str
    slug: str
    branch: str
    base_branch: str
    pipeline: PipelineRef
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    status: str = RUN_RUNNING
    current_step: str | None = None
    # Non-fatal anomalies surfaced rather than swallowed (data over inference) —
    # e.g. a required final-gate artifact (FR-9.8 PR.md) that could not be
    # rendered. Recorded so a completed run never silently hides a missing
    # deliverable (review F-005).
    warnings: list[str] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    commits: list[CommitRecord] = Field(default_factory=list)
    totals: UsageTotals = Field(default_factory=UsageTotals)
    # Per-agent-profile usage (FR-3.2): `gauntlet report` needs the spend split
    # by profile, not just by step — a single adversarial_cycle step bills the
    # reviewer, triager, fixer, and escalation profiles, so step-level totals
    # alone cannot answer "is triage < 5% of run cost?" (FR-3 acceptance).
    agent_usage: dict[str, UsageTotals] = Field(default_factory=dict)

    # ---- record lookup -------------------------------------------------------
    def record(self, step_id: str, iteration: str | None = None) -> StepRecord | None:
        for rec in self.steps:
            if rec.id == step_id and rec.iteration == iteration:
                return rec
        return None

    def upsert(self, rec: StepRecord) -> StepRecord:
        for i, existing in enumerate(self.steps):
            if existing.id == rec.id and existing.iteration == rec.iteration:
                self.steps[i] = rec
                return rec
        self.steps.append(rec)
        return rec

    # ---- atomic persistence (FR-8.2) ----------------------------------------
    def write_atomic(self, path: Path) -> None:
        """Write the manifest atomically: temp file in the same dir + replace.

        ``os.replace`` is atomic on POSIX within a filesystem, so a reader (or a
        resume after kill) always sees a whole manifest — the prior one until
        the instant the new one lands. ``fsync`` before replace so the bytes are
        durable, not just in the page cache, before the rename is visible.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            # On any failure (including KeyboardInterrupt) leave the prior
            # manifest untouched and clean up the temp file.
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_text())
