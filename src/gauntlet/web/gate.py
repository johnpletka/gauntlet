"""Gate / escalation resolution + deterministic phase diff (P5, FR-4).

Brings the human decision *and its evidence* to one place (G3). Two parked
shapes are resolved here, both **read-only** and engine-free (D6):

- **human_gate** (FR-4.1/4.2/4.3) — resolve the gate's ``show:`` list (read from
  the run's snapshot ``pipeline.yaml``) into rendered content, each name resolved
  first against ``run_dir/artifacts/<name>`` (where cycles write
  ``findings.json``/``triage.json``/``confirm.json``) then the slug-dir artifact
  root (``prd.md``/``plan.md``). Offers Approve / Reject (FR-4.4).
- **adversarial_cycle escalation** (FR-4.6, EXP-1) — a run parked *inside* a
  cycle whose ``notes`` begin with ``escalation``. This is **not** a gate and the
  engine offers no approve/reject for it; resolve the escalated finding(s) +
  their triage verdicts and frame the reconcile-then-Resume decision.

The phase **diff** (FR-4.3) is selected deterministically from
``manifest.commits[]`` so every reviewer sees the same range; the empty case
returns an explicit "no committed diff" sentinel and falls back to the artifact
content, never a misleading empty/whole-repo diff.

All file reads go through :class:`~gauntlet.web.store.RunStore` containment
(reject ``..``/escape, FR-10.1 / review F-006).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from gauntlet.engine import gitops
from gauntlet.engine.manifest import PARKED, Manifest
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.web.intel import extract_finding_ids
from gauntlet.web.store import RunNotFound, RunStore, UnsafePath, _safe_segment

# A git rev we are willing to hand to `git diff` (FR-10.1 containment): a hex sha
# (optionally abbreviated) with optional `^`/`~N` suffixes. Rejects anything that
# could be a flag (`--upload-pack=…`) or a path — never an arbitrary string.
_REV_RE = re.compile(r"^[0-9a-fA-F]{4,40}(?:[\^~][0-9]*)*$")

# A document gate (plan/PRD cycle) shows these; everything else renders as a
# generic markdown/JSON artifact.
_DOC_ARTIFACTS = {"prd.md", "plan.md"}


class GateArtifact(BaseModel):
    """One resolved ``show:`` artifact (FR-4.3)."""

    name: str
    kind: str  # findings | triage | confirm | json | markdown | text | missing
    source: str | None = None  # "artifacts" | "slug" — where it resolved
    parsed: Any | None = None  # structured payload for *.json
    content: str | None = None  # raw text for markdown/text
    error: str | None = None  # set when present-but-unparseable


class EscalatedFinding(BaseModel):
    """One escalated finding merged with its triage verdict (FR-4.6)."""

    finding_id: str
    severity: str | None = None
    category: str | None = None
    location: str | None = None
    claim: str | None = None
    evidence: str | None = None
    suggested_fix: str | None = None
    # from triage:
    verdict: str | None = None
    action: str | None = None
    reasoning: str | None = None
    target_artifact: str | None = None
    confidence: str | None = None


class GateView(BaseModel):
    """Resolved gate or escalation-park decision (§6 ``/gate``)."""

    slug: str
    run_id: str
    gate_id: str
    gate_type: str  # "human_gate" | "adversarial_cycle"
    kind: str  # "gate" | "escalation"
    notes: str | None = None
    artifacts: list[GateArtifact] = []
    # escalation-only (FR-4.6):
    escalated_finding_ids: list[str] = []
    escalated: list[EscalatedFinding] = []
    target_artifacts: list[str] = []
    # FR-4.5 upstream-conflict text from the transcript, when the gate notes
    # indicate the builder signalled one.
    upstream_conflict: str | None = None


class DiffView(BaseModel):
    """Resolved phase diff (§6 ``/diff``, FR-4.3)."""

    slug: str
    run_id: str
    phase: str | None = None
    from_sha: str | None = None
    to_sha: str | None = None
    no_committed_diff: bool = False
    diff: str | None = None
    log: str | None = None
    # The artifact-content fallback for the empty case (FR-4.3).
    fallback: GateArtifact | None = None


class NoPendingGate(LookupError):
    """The run is not parked at a human_gate or a cycle escalation (→ 404)."""


# --- artifact rendering ------------------------------------------------------


def _classify_json(name: str, data: Any) -> str:
    # Explicit filenames win over the structural heuristic (review F-003):
    # confirm.json also carries a `verdicts` key, so the heuristic alone would
    # mis-type it as triage and it would never render with its own table.
    if name == "findings.json":
        return "findings"
    if name == "triage.json":
        return "triage"
    if name == "confirm.json":
        return "confirm"
    if isinstance(data, dict) and "findings" in data:
        return "findings"
    if isinstance(data, dict) and "verdicts" in data:
        return "triage"
    return "json"


def _render_artifact(name: str, path: Path | None, source: str | None) -> GateArtifact:
    if path is None or not path.exists() or not path.is_file():
        return GateArtifact(name=name, kind="missing", source=source)
    try:
        text = path.read_text()
    except OSError as exc:  # pragma: no cover - defensive
        return GateArtifact(name=name, kind="missing", source=source, error=str(exc))
    if name.endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            return GateArtifact(
                name=name, kind="json", source=source, content=text, error=str(exc)
            )
        return GateArtifact(
            name=name, kind=_classify_json(name, data), source=source, parsed=data
        )
    kind = "markdown" if name.endswith(".md") else "text"
    return GateArtifact(name=name, kind=kind, source=source, content=text)


class GateResolver:
    """Resolve a parked gate/escalation and select its phase diff (FR-4)."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    # ---- artifact path resolution (FR-4.2 / review F-006) -------------------
    def _resolve_artifact(
        self, run_dir: Path, slug_dir: Path, name: str
    ) -> tuple[Path | None, str | None]:
        """``show:`` name → (path, source), first artifacts/ then slug dir.

        The name is pipeline-/user-selected, so it is validated as a single safe
        segment and each candidate is asserted to stay within its root (FR-10.1).
        A name that resolves nowhere returns ``(None, None)`` → rendered as a
        ``missing`` artifact, not an error (a gate may list a not-yet-written
        artifact).
        """
        _safe_segment(name, kind="show")
        cand = self.store._assert_within(run_dir / "artifacts" / name, run_dir)
        if cand.exists() and cand.is_file():
            return cand, "artifacts"
        slug_cand = self.store._assert_within(slug_dir / name, slug_dir)
        if slug_cand.exists() and slug_cand.is_file():
            return slug_cand, "slug"
        return None, None

    def _gate_step(self, man: Manifest) -> Any:
        """The parked step ``current_step`` points at, or raise NoPendingGate."""
        sid = man.current_step
        if not sid:
            raise NoPendingGate("run has no current step")
        rec = man.record(sid)
        if rec is None:
            # foreach fan-out: id without iteration — take the parked match.
            matches = [r for r in man.steps if r.id == sid and r.status == PARKED]
            rec = matches[-1] if matches else man.record(sid, None)
        if rec is None:
            raise NoPendingGate(f"no record for current step {sid!r}")
        return rec

    # ---- the gate view (FR-4.1/4.2/4.6) ------------------------------------
    def gate(self, slug: str, run_id: str | None = None) -> GateView:
        run_dir = self.store.run_dir(slug, run_id)
        rid = run_dir.name
        man = Manifest.load(run_dir / "manifest.json")
        rec = self._gate_step(man)
        slug_dir = self.store._slug_dir(slug)

        if rec.type == "human_gate" and rec.status == PARKED:
            return self._human_gate_view(slug, rid, run_dir, slug_dir, man, rec)
        if (
            rec.type == "adversarial_cycle"
            and rec.status == PARKED
            and (rec.notes or "").lower().lstrip().startswith("escalation")
        ):
            return self._escalation_view(slug, rid, run_dir, man, rec)
        raise NoPendingGate(
            f"step {rec.id!r} ({rec.type}, {rec.status}) is not a pending gate "
            "or cycle escalation"
        )

    def _human_gate_view(
        self, slug, rid, run_dir, slug_dir, man, rec
    ) -> GateView:
        show = self._show_list(run_dir, rec.id)
        artifacts = [
            _render_artifact(name, *self._resolve_artifact(run_dir, slug_dir, name))
            for name in show
        ]
        upstream = None
        if "upstream conflict" in (rec.notes or "").lower():
            upstream = self._upstream_conflict_text(run_dir, rec.id)
        return GateView(
            slug=slug,
            run_id=rid,
            gate_id=rec.id,
            gate_type="human_gate",
            kind="gate",
            notes=rec.notes,
            artifacts=artifacts,
            upstream_conflict=upstream,
        )

    def _escalation_view(self, slug, rid, run_dir, man, rec) -> GateView:
        slug_dir = self.store._slug_dir(slug)
        fids = extract_finding_ids(rec.notes)
        findings_art = _render_artifact(
            "findings.json",
            *self._resolve_artifact(run_dir, slug_dir, "findings.json"),
        )
        triage_art = _render_artifact(
            "triage.json", *self._resolve_artifact(run_dir, slug_dir, "triage.json")
        )
        by_id = self._index_findings(findings_art)
        verdicts = self._index_verdicts(triage_art)
        escalated: list[EscalatedFinding] = []
        targets: list[str] = []
        # If the note named no ids (defensive), fall back to every finding that
        # has an upstream target or a blocking verdict — the escalation set.
        ids = fids or list(by_id)
        for fid in ids:
            f = by_id.get(fid, {})
            v = verdicts.get(fid, {})
            if v.get("target_artifact"):
                targets.append(v["target_artifact"])
            escalated.append(
                EscalatedFinding(
                    finding_id=fid,
                    severity=f.get("severity"),
                    category=f.get("category"),
                    location=f.get("location"),
                    claim=f.get("claim"),
                    evidence=f.get("evidence"),
                    suggested_fix=f.get("suggested_fix"),
                    verdict=v.get("verdict"),
                    action=v.get("action"),
                    reasoning=v.get("reasoning"),
                    target_artifact=v.get("target_artifact"),
                    confidence=v.get("confidence"),
                )
            )
        return GateView(
            slug=slug,
            run_id=rid,
            gate_id=rec.id,
            gate_type="adversarial_cycle",
            kind="escalation",
            notes=rec.notes,
            artifacts=[findings_art, triage_art],
            escalated_finding_ids=fids,
            escalated=escalated,
            target_artifacts=list(dict.fromkeys(targets)),
        )

    @staticmethod
    def _index_findings(art: GateArtifact) -> dict[str, dict]:
        data = art.parsed if isinstance(art.parsed, dict) else {}
        out: dict[str, dict] = {}
        for f in data.get("findings", []) or []:
            if isinstance(f, dict) and f.get("id"):
                out[f["id"]] = f
        return out

    @staticmethod
    def _index_verdicts(art: GateArtifact) -> dict[str, dict]:
        data = art.parsed if isinstance(art.parsed, dict) else {}
        out: dict[str, dict] = {}
        for v in data.get("verdicts", []) or []:
            if isinstance(v, dict) and v.get("finding_id"):
                out[v["finding_id"]] = v
        return out

    def _show_list(self, run_dir: Path, step_id: str) -> list[str]:
        """The gate step's ``show:`` list, read from the snapshot pipeline.

        Reads the run's *own* snapshot ``pipeline.yaml`` (FR-4.2) so the gate is
        resolved against the pipeline the run actually committed to, not the
        repo's current one. A missing/unparseable snapshot yields an empty list
        (the gate renders with no artifacts rather than crashing).
        """
        try:
            pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
        except (FileNotFoundError, ValueError):
            return []
        for step in pipeline.all_steps():
            if step.id == step_id:
                return list(step.get("show", []) or [])
        return []

    def _upstream_conflict_text(self, run_dir: Path, step_id: str) -> str | None:
        """Best-effort: the builder's UPSTREAM CONFLICT block from a transcript.

        Read-only and fail-soft — the gate is usable even if the transcript is
        absent. Scoped to the step dir (containment)."""
        try:
            _safe_segment(step_id, kind="step")
            step_dir = self.store._assert_within(
                run_dir / "steps" / step_id, run_dir
            )
        except UnsafePath:
            return None
        if not step_dir.exists():
            return None
        for cand in ("transcript.md", "events.jsonl"):
            p = step_dir / cand
            if not p.exists() or not p.is_file():
                continue
            try:
                text = p.read_text()
            except OSError:  # pragma: no cover - defensive
                continue
            idx = text.upper().find("UPSTREAM CONFLICT")
            if idx != -1:
                return text[idx : idx + 4000]
        return None

    # ---- deterministic phase diff (FR-4.3) ---------------------------------
    def diff(
        self,
        slug: str,
        *,
        run_id: str | None = None,
        from_sha: str | None = None,
        to_sha: str | None = None,
    ) -> DiffView:
        run_dir = self.store.run_dir(slug, run_id)
        rid = run_dir.name
        man = Manifest.load(run_dir / "manifest.json")
        repo = self.store.repo_root

        # Explicit SHAs override the automatic selection (§6). Validate them as
        # revs so they can never be a flag/path injected into `git diff`.
        if from_sha or to_sha:
            if not from_sha or not to_sha:
                raise UnsafePath("diff requires both `from` and `to`, or neither")
            self._assert_rev(from_sha)
            self._assert_rev(to_sha)
            return DiffView(
                slug=slug,
                run_id=rid,
                from_sha=from_sha,
                to_sha=to_sha,
                diff=gitops.range_diff(repo, from_sha, to_sha),
                log=gitops.log_range(repo, from_sha, to_sha),
            )

        sel = self._select_phase_range(man)
        if sel is None:
            # Empty / no-diff case (FR-4.3): no commits for the gated phase. Fall
            # back to the artifact content so the reviewer still sees *something*
            # rather than a misleading empty/whole-repo diff.
            fallback = self._diff_fallback(slug, run_dir, man)
            return DiffView(
                slug=slug, run_id=rid, no_committed_diff=True, fallback=fallback
            )
        phase, frm, to = sel
        return DiffView(
            slug=slug,
            run_id=rid,
            phase=phase,
            from_sha=frm,
            to_sha=to,
            diff=gitops.range_diff(repo, frm, to),
            log=gitops.log_range(repo, frm, to),
        )

    @staticmethod
    def _assert_rev(rev: str) -> None:
        if not _REV_RE.match(rev):
            raise UnsafePath(f"unsafe git rev: {rev!r}")

    def _select_phase_range(self, man: Manifest):
        """``(phase, from_sha, to_sha)`` for the current gate, or None if empty.

        Groups ``manifest.commits[]`` by the ``PN`` base of each entry's
        ``phase`` (so ``PN`` + ``PN.1`` + ``PN.2`` — the phase commit plus every
        post-review fix round — group together). The gated phase is the base of
        the **last** commit (the most recent phase committed). ``to`` = that
        group's last sha; ``from`` = the commit immediately preceding the group,
        or the committing step's ``base_sha`` when the group is first to commit.
        """
        commits = man.commits
        if not commits:
            return None
        base_of = lambda c: c.phase.split(".")[0]
        gated_base = base_of(commits[-1])
        group = [c for c in commits if base_of(c) == gated_base]
        if not group:
            return None
        to = group[-1].sha
        first = group[0]
        first_idx = commits.index(first)
        if first_idx > 0:
            frm = commits[first_idx - 1].sha
        else:
            # First committing phase: diff against the state before it began.
            rec = man.record(first.step_id)
            if rec is not None and rec.base_sha:
                frm = rec.base_sha
            else:
                # Deterministic fallback with no new git helper: the first
                # parent of the phase's first commit (FR-4.3 intent).
                frm = f"{first.sha}^"
        return gated_base, frm, to

    def _diff_fallback(self, slug, run_dir, man) -> GateArtifact | None:
        """Artifact content for the no-committed-diff case (FR-4.3).

        Prefer the gate's first ``show:`` artifact (e.g. the plan/PRD for a
        document gate, the findings for an impl gate); fall back to whatever the
        slug dir holds. ``None`` when nothing is resolvable."""
        slug_dir = self.store._slug_dir(slug)
        names: list[str] = []
        sid = man.current_step
        if sid:
            names = self._show_list(run_dir, sid)
        for name in names or list(_DOC_ARTIFACTS):
            path, source = self._resolve_artifact(run_dir, slug_dir, name)
            if path is not None:
                return _render_artifact(name, path, source)
        return None


class HandoffView(BaseModel):
    """A ready-to-run, read-only scoped-analysis prompt (FR-4.7).

    The console **spawns nothing and makes no model call** — it only assembles
    copy-pasteable context (D5/D8). Off by default; enabled via config.
    """

    slug: str
    run_id: str
    gate_id: str
    prompt: str
    invocation: str


def handoff_prompt(view: GateView) -> HandoffView:
    """Assemble the FR-4.7 read-only analysis prompt for a gate/escalation.

    Pure string assembly over the already-resolved :class:`GateView`: run_id,
    parked step, the escalated finding(s) + triage verdicts, the named upstream
    artifact(s), and the reconciliation options. The accompanying invocation runs
    it in a terminal; the console never executes it (FR-10.1)."""
    lines: list[str] = [
        "You are a read-only analysis assistant. Do NOT edit files, run git, or "
        "call any gauntlet verb. Explain the parked decision below and lay out "
        "the operator's reconciliation options; recommend nothing destructive.",
        "",
        f"Run: {view.slug} / {view.run_id}",
        f"Parked step: {view.gate_id} ({view.gate_type}, kind={view.kind})",
        f"Engine notes: {view.notes or '(none)'}",
    ]
    if view.upstream_conflict:
        lines += ["", "UPSTREAM CONFLICT (from transcript):", view.upstream_conflict]
    if view.kind == "escalation":
        if view.target_artifacts:
            lines += ["", "Upstream artifact(s) targeted: " + ", ".join(view.target_artifacts)]
        lines += ["", "Escalated findings:"]
        for ef in view.escalated:
            lines += [
                f"- {ef.finding_id} [{ef.severity}/{ef.category}] @ {ef.location}",
                f"    claim: {ef.claim}",
                f"    evidence: {ef.evidence}",
                f"    triage: verdict={ef.verdict} action={ef.action} "
                f"target={ef.target_artifact} confidence={ef.confidence}",
                f"    reasoning: {ef.reasoning}",
            ]
        lines += [
            "",
            "Reconciliation options: (a) amend the named approved artifact "
            "(PRD/plan) through human ratification, or (b) apply the in-code fix; "
            "THEN resume. A bare resume re-runs the cycle and re-parks.",
        ]
    else:
        lines += ["", "Gate artifacts shown:"]
        for a in view.artifacts:
            lines.append(f"- {a.name} ({a.kind})")
        lines += [
            "",
            "Decision options: Approve (continue) or Reject (with notes).",
        ]
    return HandoffView(
        slug=view.slug,
        run_id=view.run_id,
        gate_id=view.gate_id,
        prompt="\n".join(lines),
        invocation=(
            f"claude  # paste the assembled prompt above; review "
            f"runs/{view.slug}/{view.run_id}/ read-only"
        ),
    )


__all__ = [
    "GateResolver",
    "GateView",
    "GateArtifact",
    "EscalatedFinding",
    "DiffView",
    "HandoffView",
    "handoff_prompt",
    "NoPendingGate",
]
