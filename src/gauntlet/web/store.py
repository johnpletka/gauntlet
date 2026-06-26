"""RunStore — the console's read model (P1, FR-1/FR-2).

Read-only. Discovers every slug under the configured run root and parses each
``manifest.json`` via the existing pydantic :class:`Manifest`. It never imports
the orchestrator drive path and never writes — the load-bearing P1 premise is
that the on-disk manifest + artifact layout is a *sufficient* read model with
**zero** engine changes (D2, FR-11.2).

Containment (FR-10.1 / §7): every user-controlled path segment (``slug``,
``run_id``, ``step``) is validated as a single safe filename and the resolved
path is asserted to live under the run root, so no request can read outside the
repo tree.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import (
    RUN_ABORTED,
    RUN_DONE,
    RUN_FAILED,
    DONE,
    FAILED,
    HALTED,
    SKIPPED,
    Manifest,
    StepRecord,
)

# Run-level states that mean a run has finished (so an `ended` is meaningful).
_TERMINAL_RUN_STATES = frozenset({RUN_DONE, RUN_ABORTED, RUN_FAILED})

# Step states that mean an iteration has reached an end. Used to pick the
# *active* iteration of a foreach fan-out (review F-003): `parked`/`interrupted`
# are not terminal — they are the current iteration awaiting a human/resume.
_TERMINAL_STEP_STATES = frozenset({DONE, FAILED, HALTED, SKIPPED})


class UnsafePath(ValueError):
    """A user-supplied path segment is not a safe single filename (FR-10.1)."""


class RunNotFound(LookupError):
    """No run matches the requested slug / run_id (maps to HTTP 404)."""


# Log files a step may expose for tailing (P2, FR-3.2). A *fixed allowlist* — the
# `name` query param is user-controlled, so it must never resolve to an arbitrary
# file inside the step dir (containment, FR-10.1 / review F-006). The default tail
# target is the first of these that exists. The supervisor's `.serve/…log`
# (FR-3.3) is owned-run only and lands in P3.
ALLOWED_LOG_NAMES = ("events.jsonl", "transcript.md")
# Cap a single tail read so one request can never pull an unbounded step log into
# memory (R5: byte-offset deltas, capped backfill).
DEFAULT_LOG_MAX_BYTES = 256 * 1024


# --- view models -------------------------------------------------------------


class RunRow(BaseModel):
    """One run-list row (§6 ``GET /api/runs`` contract, FR-1.1).

    ``owned``/``attached`` are always ``False`` in P1 — there is no supervisor
    yet (P3), so every discovered run is *observed*. ``updated`` is the
    manifest's last-write time, the "age/last-update" column of FR-1.1.
    """

    slug: str
    run_id: str
    status: str
    current_step: str | None = None
    current_step_status: str | None = None
    current_step_notes: str | None = None
    started: str | None = None
    ended: str | None = None
    totals: dict = {}
    branch: str | None = None
    base_branch: str | None = None
    owned: bool = False
    attached: bool = False
    # FR-1.4/FR-10.5: this run is believed to be actively driven by a live
    # process the console does not own (it holds the worktree lock). Such a run
    # shows "running (external)" and its driving controls are disabled.
    external: bool = False
    n_steps: int = 0
    n_done: int = 0
    warnings_count: int = 0
    updated: str | None = None


class StepArtifact(BaseModel):
    name: str
    size: int
    path: str  # path relative to the step dir (the page's ?artifact= value)


class StepRound(BaseModel):
    """A nested dir under a step — a cycle's ``r1-review`` / a retrospective's
    ``synthesis`` — possibly itself containing further round dirs (e.g. a
    triage round's per-finding ``F-001`` dirs)."""

    name: str
    path: str  # path relative to the step dir
    artifacts: list[StepArtifact] = []
    rounds: list["StepRound"] = []


class StepDetail(BaseModel):
    slug: str
    run_id: str
    step: str
    type: str | None = None
    status: str | None = None
    agent: str | None = None
    iteration: str | None = None
    artifacts: list[StepArtifact] = []
    rounds: list[StepRound] = []


class LogChunk(BaseModel):
    """A byte-offset slice of a step log (P2, FR-3.2).

    The client tails by re-requesting with ``from=<end>`` of the prior chunk.
    ``start`` is the offset actually read from — it differs from the requested
    ``from`` only when the file shrank/rotated under us (``from`` past EOF), in
    which case we reset to 0 and the client adopts ``start`` as its new cursor.
    """

    name: str
    start: int
    end: int
    size: int
    eof: bool
    text: str


def _safe_segment(seg: str, *, kind: str) -> str:
    """Reject anything that is not a single, traversal-free filename.

    The first line of defence for FR-10.1 containment: a segment may not be
    empty, ``.``/``..``, or contain a path separator or NUL. The resolved path
    is *additionally* asserted to live under the run root (belt and suspenders).
    """
    if not seg or seg in (".", "..") or "/" in seg or "\\" in seg or "\x00" in seg:
        raise UnsafePath(f"unsafe {kind} segment: {seg!r}")
    return seg


def _current_record(man: Manifest) -> StepRecord | None:
    """The step record `current_step` points at, or None.

    A ``foreach`` fan-out stores several records under one ``id`` (one per
    ``iteration``) while the manifest's ``current_step`` carries only the id,
    not the iteration (review F-003). Prefer the *active* (non-terminal) matching
    record so live state reflects the running iteration rather than a completed
    earlier one; fall back to the last matching record when every iteration is
    terminal. For an ordinary single-record step this returns that record.
    """
    if not man.current_step:
        return None
    matches = [rec for rec in man.steps if rec.id == man.current_step]
    if not matches:
        return None
    active = [rec for rec in matches if rec.status not in _TERMINAL_STEP_STATES]
    return active[-1] if active else matches[-1]


def _started_ended(man: Manifest) -> tuple[str | None, str | None]:
    starts = [s.started for s in man.steps if s.started]
    started = starts[0] if starts else None
    ended: str | None = None
    if man.status in _TERMINAL_RUN_STATES:
        ends = [s.ended for s in man.steps if s.ended]
        ended = ends[-1] if ends else None
    return started, ended


def _mtime_iso(path: Path) -> str | None:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat()


def duration_seconds(started: str | None, ended: str | None) -> float | None:
    """Wall-clock seconds between two ISO timestamps, or None if unparseable."""
    if not started or not ended:
        return None
    try:
        a = datetime.fromisoformat(started)
        b = datetime.fromisoformat(ended)
    except ValueError:
        return None
    return (b - a).total_seconds()


class RunStore:
    """Read-only view over a repo's runs (one ``serve`` instance = one repo).

    An optional :class:`~gauntlet.web.supervisor.JobSupervisor` (P3) lets the
    store mark runs the console launched as **owned** (FR-1.4) and surface the
    worktree-lock holder (FR-10.5). The store stays read-only — it only *reads*
    the supervisor's on-disk discovery; it never launches or reaps anything.
    """

    def __init__(self, repo_root: Path, config: RunConfig, *, supervisor=None) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config
        self.supervisor = supervisor
        # Belt-and-suspenders for FR-10.1 / review F-001: `RunConfig` already
        # validates `run_root` as a repo-relative, non-escaping path, but a
        # directly-constructed config (or a future loosening) could still point
        # the run root outside the repo. Fail closed at construction so no later
        # request can read outside the repo tree.
        run_root = (self.repo_root / config.run_root).resolve()
        if run_root != self.repo_root and self.repo_root not in run_root.parents:
            raise UnsafePath(
                f"run_root {config.run_root!r} escapes the repo root {self.repo_root}"
            )

    @classmethod
    def from_repo(cls, repo_root: Path, *, supervisor=None) -> "RunStore":
        """Load config like the CLI, falling back to defaults only if absent.

        ``RunConfig.load`` raises ``FileNotFoundError`` when
        ``.gauntlet/config.yaml`` is missing; the console must still serve a bare
        repo (defaults give ``run_root=runs``, ``asset_root=.``), so an *absent*
        config degrades to defaults. A *present but malformed* config (bad YAML,
        an invalid ``run_root``/``asset_root``) must fail closed instead of
        silently serving the wrong run root (review F-002, plan ground rule:
        parse errors fail closed) — those errors propagate to the caller.
        """
        try:
            config = RunConfig.load(repo_root / ".gauntlet/config.yaml")
        except FileNotFoundError:
            config = RunConfig()
        return cls(repo_root, config, supervisor=supervisor)

    # ---- layout --------------------------------------------------------------
    @property
    def run_root_dir(self) -> Path:
        return (self.repo_root / self.config.run_root).resolve()

    def _assert_within(self, path: Path, root: Path) -> Path:
        """Fail closed if ``path`` resolves outside ``root`` (FR-10.1).

        ``root`` must already be resolved. The check follows symlinks (``resolve``)
        so an allowed-name symlink cannot escape the intended subtree.
        """
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise UnsafePath(f"path escapes {root}: {path}")
        return resolved

    def _assert_contained(self, path: Path) -> Path:
        """Fail closed if ``path`` resolves outside the run root (FR-10.1)."""
        return self._assert_within(path, self.run_root_dir)

    def slugs(self) -> list[str]:
        root = self.run_root_dir
        if not root.exists():
            return []
        return sorted(
            d.name
            for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    def _slug_dir(self, slug: str) -> Path:
        _safe_segment(slug, kind="slug")
        return self._assert_contained(self.run_root_dir / slug)

    def _run_dirs(self, slug_dir: Path) -> list[str]:
        """Sorted run-dir names (lexical == chronological for ``run-<ts>``)."""
        if not slug_dir.exists():
            return []
        return sorted(
            d.name
            for d in slug_dir.glob("run-*")
            if d.is_dir() and (d / "manifest.json").exists()
        )

    def _resolve_run_id(self, slug: str, run_id: str | None) -> str:
        """Resolve to a concrete run id: explicit, else active pointer, else latest.

        An explicit unknown ``run_id`` → :class:`RunNotFound` (404, FR-2.4). With
        none supplied, prefer the live ``active-run.txt`` pointer (the "active"
        run), else the lexically-greatest ``run-*`` dir (the "latest").
        """
        slug_dir = self._slug_dir(slug)
        if run_id is not None:
            _safe_segment(run_id, kind="run_id")
            run_dir = self._assert_contained(slug_dir / run_id)
            if not (run_dir / "manifest.json").exists():
                raise RunNotFound(f"no run {run_id!r} for slug {slug!r}")
            return run_id

        pointer = slug_dir / "active-run.txt"
        if pointer.exists():
            active = pointer.read_text().strip()
            if active:
                _safe_segment(active, kind="run_id")
                if (self._assert_contained(slug_dir / active) / "manifest.json").exists():
                    return active

        names = self._run_dirs(slug_dir)
        if not names:
            raise RunNotFound(f"no runs for slug {slug!r}")
        return names[-1]

    def resolve_run_id(self, slug: str, run_id: str | None = None) -> str:
        """Public: resolve to a concrete run id (explicit → active → latest).

        Used by the live log-tail (P2) to pin a stream to one run dir before it
        starts polling, so a tail never drifts onto a different run if a newer
        one is minted mid-stream.
        """
        return self._resolve_run_id(slug, run_id)

    def run_dir(self, slug: str, run_id: str | None = None) -> Path:
        rid = self._resolve_run_id(slug, run_id)
        return self._assert_contained(self._slug_dir(slug) / rid)

    def iter_manifests(self) -> list[tuple[str, str, Path]]:
        """Every run's ``(slug, run_id, manifest_path)`` — all slugs, all history.

        The watcher (P2) stats each of these once per tick to detect transitions.
        Includes historical runs so the detail view and log tail of any past run
        also get live updates while open.
        """
        out: list[tuple[str, str, Path]] = []
        root = self.run_root_dir
        if not root.exists():
            return out
        for slug in self.slugs():
            slug_dir = root / slug
            for rid in self._run_dirs(slug_dir):
                out.append((slug, rid, slug_dir / rid / "manifest.json"))
        return out

    # ---- step log tail (FR-3.2) ---------------------------------------------
    def step_log(
        self,
        slug: str,
        step: str,
        *,
        run_id: str | None = None,
        name: str | None = None,
        offset: int = 0,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    ) -> LogChunk:
        """Read a byte-offset slice of a step's log file (FR-3.2, R5).

        ``name`` is restricted to :data:`ALLOWED_LOG_NAMES` (a user-controlled
        param must not address an arbitrary file — review F-006); when omitted,
        the first of those that exists is tailed. Bytes before ``offset`` are
        never returned, so a client re-requesting with ``from=<prior end>`` sees
        only appended bytes. If ``offset`` is past EOF (rotation/truncation) we
        reset ``start`` to 0 so the client re-syncs rather than reading garbage.
        """
        run_dir = self.run_dir(slug, run_id)
        _safe_segment(step, kind="step")
        step_dir = self._assert_contained(run_dir / "steps" / step)
        if not step_dir.exists() or not step_dir.is_dir():
            raise RunNotFound(f"no step {step!r} in run {run_dir.name!r}")

        if name is not None:
            _safe_segment(name, kind="log")
            if name not in ALLOWED_LOG_NAMES:
                raise UnsafePath(f"log {name!r} is not a tailable step log")
            candidates = [name]
        else:
            candidates = list(ALLOWED_LOG_NAMES)

        path: Path | None = None
        for cand in candidates:
            # Resolve each candidate against the *step* dir, not just the run root
            # (review F-002): an allowed-name symlink (e.g. `events.jsonl` ->
            # `../../plan.md`) stays inside the run root and would otherwise be read
            # as this step's log. Containment against `step_dir` rejects that escape.
            p = self._assert_within(step_dir / cand, step_dir)
            if p.exists() and p.is_file():
                path = p
                name = cand
                break
        if path is None:
            raise RunNotFound(
                f"no tailable log for step {step!r} in run {run_dir.name!r}"
            )

        return _read_chunk(path, name, offset, max_bytes)

    # ---- supervisor-derived ownership (P3, FR-1.4/FR-10.5) -------------------
    def _ownership(self, slug: str, run_id: str, lock) -> tuple[bool, bool, bool]:
        """``(owned, attached, external)`` for one run, given the worktree lock.

        Owned/attached come from the supervisor's on-disk ``.serve/job.json``
        discovery; ``external`` is true when a **live** lock holder is this run
        but the console does not own it (so it is driven by another process,
        FR-10.5). With no supervisor (P1/P2) every run is observed.
        """
        if self.supervisor is None:
            return (False, False, False)
        owned = self.supervisor.is_owned(slug, run_id)
        attached = self.supervisor.is_attached(slug, run_id)
        external = bool(
            lock is not None
            and lock.live
            and lock.slug == slug
            and lock.run_id == run_id
            and not owned
        )
        return (owned, attached, external)

    def worktree_lock(self):
        """The live worktree-lock holder, or ``None`` (FR-10.5 UI surface)."""
        if self.supervisor is None:
            return None
        lock = self.supervisor.driving_lock()
        return lock if (lock is not None and lock.live) else None

    # ---- owned-run captured log tail (FR-3.3) -------------------------------
    def serve_log(
        self,
        slug: str,
        *,
        run_id: str | None = None,
        offset: int = 0,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    ) -> LogChunk:
        """Tail an owned run's supervisor-captured stdout/stderr (FR-3.3).

        This is the combined log the console wrote at launch
        (``run_dir/.serve/run.log``) — "the thing that today scrolls past in a
        backgrounded terminal". Same byte-offset delta protocol as
        :meth:`step_log` (R5). 404 if the run was not console-launched (no
        ``.serve`` log). The name is a fixed constant, not user-controlled, so
        there is no traversal surface here.
        """
        run_dir = self.run_dir(slug, run_id)
        path = self._assert_within(run_dir / ".serve" / "run.log", run_dir)
        if not path.exists() or not path.is_file():
            raise RunNotFound(
                f"no captured serve log for run {run_dir.name!r} "
                "(not a console-launched run)"
            )
        return _read_chunk(path, "run.log", offset, max_bytes)

    # ---- list view (FR-1.1) --------------------------------------------------
    def _row_for(self, slug: str, lock=None) -> RunRow | None:
        try:
            rid = self._resolve_run_id(slug, None)
        except RunNotFound:
            return None
        run_dir = self._slug_dir(slug) / rid
        manifest_path = run_dir / "manifest.json"
        try:
            man = Manifest.load(manifest_path)
        except (OSError, ValueError):
            return None
        cur = _current_record(man)
        started, ended = _started_ended(man)
        owned, attached, external = self._ownership(slug, man.run_id, lock)
        return RunRow(
            slug=slug,
            run_id=man.run_id,
            status=man.status,
            current_step=man.current_step,
            current_step_status=cur.status if cur else None,
            current_step_notes=cur.notes if cur else None,
            started=started,
            ended=ended,
            totals=man.totals.model_dump(),
            branch=man.branch,
            base_branch=man.base_branch,
            owned=owned,
            attached=attached,
            external=external,
            n_steps=len(man.steps),
            n_done=sum(1 for s in man.steps if s.status == DONE),
            warnings_count=len(man.warnings),
            updated=_mtime_iso(manifest_path),
        )

    def list_rows(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        slug: str | None = None,
        sort: str | None = None,
    ) -> list[RunRow]:
        """Run-list rows, filtered/sorted per the §6 query contract (FR-1.2).

        One row per slug (its latest/active run). ``status`` keeps only that run
        status; ``slug`` is a case-insensitive substring on the slug; ``q`` is a
        free-text substring over slug **and** branch. ``sort`` is ``slug`` /
        ``status`` (else most-recently-updated first). Filtering happens in the
        read model, not the UI, so the JSON API and the server-rendered page
        agree.
        """
        # Reap finished console children before computing liveness so a
        # completed owned run is never shown as still live/attached via an
        # unreaped zombie's PID-liveness (review F-004). Read-only stores (no
        # supervisor) and minimal stubs without `reap` are left untouched.
        reap = getattr(self.supervisor, "reap", None)
        if callable(reap):
            reap()
        lock = self.worktree_lock()
        rows = [
            r for s in self.slugs() if (r := self._row_for(s, lock)) is not None
        ]
        return _filter_sort_rows(rows, status=status, q=q, slug=slug, sort=sort)

    # ---- full-history browser (FR-2.4) --------------------------------------
    def run_history(self, slug: str) -> list[dict]:
        """Every run of ``slug`` (newest first) for the history browser (FR-2.4).

        One entry per ``run-<timestamp>`` dir with a readable manifest:
        ``{run_id, status, current_step, started, ended, updated}``. A run with
        an unreadable manifest is skipped rather than failing the whole list
        (surface what parses). The active run id (``active-run.txt``) is flagged
        so the UI can mark "latest/active". This is what makes a completed or
        failed PRD run reviewable long after it finished.
        """
        slug_dir = self._slug_dir(slug)
        try:
            active = self._resolve_run_id(slug, None)
        except RunNotFound:
            return []
        out: list[dict] = []
        for rid in self._run_dirs(slug_dir):
            manifest_path = slug_dir / rid / "manifest.json"
            try:
                man = Manifest.load(manifest_path)
            except (OSError, ValueError):
                continue
            started, ended = _started_ended(man)
            cur = _current_record(man)
            out.append(
                {
                    "run_id": rid,
                    "status": man.status,
                    "current_step": man.current_step,
                    "current_step_status": cur.status if cur else None,
                    "started": started,
                    "ended": ended,
                    "updated": _mtime_iso(manifest_path),
                    "active": rid == active,
                }
            )
        out.sort(key=lambda e: e["run_id"], reverse=True)
        return out

    # ---- cost report (FR-2.4 / §6) ------------------------------------------
    def report_text(self, slug: str, *, run_id: str | None = None) -> str:
        """Render the per-step/per-agent cost breakdown for a run (§6 report).

        Reuses the existing engine report renderer over the same manifest the
        ``gauntlet report`` CLI prints, so the console's cost view and the CLI
        agree byte-for-byte.
        """
        from gauntlet.engine.report import render_report

        return render_report(self.manifest(slug, run_id))

    # ---- detail view (FR-2.1) ------------------------------------------------
    def manifest(self, slug: str, run_id: str | None = None) -> Manifest:
        run_dir = self.run_dir(slug, run_id)
        try:
            return Manifest.load(run_dir / "manifest.json")
        except (OSError, ValueError) as exc:
            raise RunNotFound(f"unreadable manifest for {slug!r}: {exc}") from exc

    # ---- judge-audit view (FR-3.4) ------------------------------------------
    def judge_audit(
        self, slug: str, *, run_id: str | None = None, max_entries: int = 2000
    ) -> list[dict]:
        """Parsed ``judge-audit.jsonl`` decisions for a run (FR-3.4).

        Returns one dict per audit line (tool / decision / source / rationale /
        latency as the engine wrote them) so a judge-driven denial is
        diagnosable in a readable view. A missing audit file → empty list (a run
        with ``--no-judge`` or no tool calls writes none). The path is a fixed
        constant under the run dir, so there is no traversal surface. Malformed
        lines are skipped rather than failing the whole view (data over
        inference: surface what parses)."""
        run_dir = self.run_dir(slug, run_id)
        path = self._assert_within(run_dir / "judge-audit.jsonl", run_dir)
        if not path.exists() or not path.is_file():
            return []
        out: list[dict] = []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
                if len(out) >= max_entries:
                    break
        return out

    # ---- improvement proposals (read-only view, FR-6.4 / §2.2) --------------
    def proposals(self, slug: str, *, run_id: str | None = None) -> list[dict]:
        """A run's improvement proposals, read-only (FR-6.4 / §2.2 "not an editor").

        Reads from the **same** canonical source the CLI's ``gauntlet proposals
        review`` uses — the per-proposal markdown files under
        ``run_dir/retro/proposals/`` (parsed by ``engine.proposals``) — so the
        console view and the CLI agree on what exists. The path is a fixed
        constant under the contained run dir (no traversal surface beyond the
        already-validated ``slug``/``run_id``). An absent dir → empty list. The
        view is strictly read-only; control stays the CLI verb.
        """
        from gauntlet.engine.proposals import parse_proposal as _parse_proposal

        run_dir = self.run_dir(slug, run_id)
        proposals_dir = self._assert_within(
            run_dir / "retro" / "proposals", run_dir
        )
        out: list[dict] = []
        if not proposals_dir.is_dir():
            return out
        # Proposal files are agent-authored, so the read must be contained
        # (FR-10.1/F-003): `parse_proposal` does a bare `read_text()` that follows
        # symlinks, so a symlinked `*.md` could otherwise render any server-
        # readable file. Reject symlinks outright and re-assert each resolved path
        # stays under the proposals dir before parsing — same posture as
        # `read_step_artifact`. (Mirrors `engine.proposals.list_proposals`'s
        # sorted-glob, with containment added.)
        for path in sorted(proposals_dir.glob("*.md")):
            if path.is_symlink():
                continue
            try:
                self._assert_within(path, proposals_dir)
            except UnsafePath:
                continue
            p = _parse_proposal(path)
            out.append(
                {
                    "name": p.name,
                    "slug": p.slug,
                    "status": p.status,
                    "target_path": ", ".join(p.targets) if p.targets else "",
                    "targets": list(p.targets),
                    "rationale": p.rationale,
                    "diff": p.diff,
                    "valid": p.valid,
                    "invalid_reason": p.invalid_reason,
                    "source_run": p.source_run,
                }
            )
        return out

    def read_step_artifact(
        self,
        slug: str,
        step: str,
        relpath: str,
        *,
        run_id: str | None = None,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    ) -> str:
        """Read a file under a step's dir as text, path-contained (FR-10.1).

        ``relpath`` is the artifact's path *relative to the step dir* — a single
        name (``transcript.md``) or a nested one (``r1-review/findings.json``,
        ``r1-triage/F-001/verdict.json``) for an artifact inside a round / sub-
        step dir. It is user-controlled (the page's ``?artifact=`` query), so the
        security boundary is **containment, not an allowlist**: every path
        segment is validated (no ``.``/``..``/separator/NUL), the joined path is
        ``resolve``-checked to stay within the step dir (so an allowed-name
        symlink cannot escape — same posture as :meth:`step_log`), and only a
        regular file is served. Any file the run itself wrote under its own step
        dir is legitimately viewable (FR-3.1); nothing outside it ever is — and
        an allowlist of "known" names adds no containment over that while
        silently hiding real nested artifacts. Capped like a log tail (R5).
        """
        run_dir = self.run_dir(slug, run_id)
        _safe_segment(step, kind="step")
        step_dir = self._assert_contained(run_dir / "steps" / step)
        target = step_dir
        for seg in relpath.split("/"):
            target = target / _safe_segment(seg, kind="artifact")
        # `_assert_within` resolves symlinks: an escaping component (any symlink
        # pointing outside the step dir) already fails closed there. A symlink
        # that resolves *within* the step dir is just another in-step file.
        path = self._assert_within(target, step_dir)
        if not path.exists() or not path.is_file():
            raise RunNotFound(f"artifact {relpath!r} not found for step {step!r}")
        with path.open("rb") as fh:
            raw = fh.read(max_bytes)
        return raw.decode("utf-8", errors="replace")

    def step_detail(
        self, slug: str, step: str, run_id: str | None = None
    ) -> StepDetail:
        """Which artifacts a step has on disk, and (for cycles) its round dirs."""
        run_dir = self.run_dir(slug, run_id)
        rid = run_dir.name
        man = Manifest.load(run_dir / "manifest.json")
        _safe_segment(step, kind="step")
        steps_root = self._assert_contained(run_dir / "steps")
        step_dir = self._assert_contained(steps_root / step)
        if not step_dir.exists() or not step_dir.is_dir():
            raise RunNotFound(f"no step {step!r} in run {rid!r}")

        # Match the record by exact id, then by the pre-iteration prefix
        # (a foreach step's dir is `<id>.<iteration>` while its record id is `<id>`).
        rec = next((r for r in man.steps if r.id == step), None)
        if rec is None:
            base = step.split(".")[0]
            rec = next((r for r in man.steps if r.id == base), None)

        artifacts = _list_artifacts(step_dir)
        rounds = _list_rounds(step_dir)
        return StepDetail(
            slug=slug,
            run_id=rid,
            step=step,
            type=rec.type if rec else None,
            status=rec.status if rec else None,
            agent=rec.agent if rec else None,
            iteration=rec.iteration if rec else None,
            artifacts=artifacts,
            rounds=rounds,
        )


def _filter_sort_rows(
    rows: list[RunRow],
    *,
    status: str | None,
    q: str | None,
    slug: str | None,
    sort: str | None,
) -> list[RunRow]:
    """Apply the §6 list filters + sort (FR-1.2). Pure over the row list."""
    if status:
        want = status.strip().lower()
        rows = [r for r in rows if r.status.lower() == want]
    if slug:
        needle = slug.strip().lower()
        rows = [r for r in rows if needle in r.slug.lower()]
    if q:
        needle = q.strip().lower()
        rows = [
            r
            for r in rows
            if needle in r.slug.lower() or needle in (r.branch or "").lower()
        ]
    key = (sort or "").strip().lower()
    if key == "slug":
        rows.sort(key=lambda r: r.slug)
    elif key == "status":
        rows.sort(key=lambda r: (r.status, r.updated or ""), reverse=False)
    else:  # default: most-recently-updated first
        rows.sort(key=lambda r: (r.updated or "", r.slug), reverse=True)
    return rows


def _read_chunk(path: Path, name: str, offset: int, max_bytes: int) -> LogChunk:
    """Read a byte-offset slice of ``path`` as a :class:`LogChunk` (R5).

    Bytes before ``offset`` are never returned, so a client re-requesting with
    ``from=<prior end>`` sees only appended bytes. If ``offset`` is past EOF
    (rotation/truncation) ``start`` resets to 0 so the client re-syncs.

    Delegates the offset framing to :func:`operator.read_log_chunk` so the
    console SSE tail and ``gauntlet logs --follow`` read identically (plan P3,
    "so CLI and console agree"). Lazy-imported to avoid any import-time coupling.
    """
    from gauntlet.engine.operator import read_log_chunk

    chunk = read_log_chunk(path, offset, max_bytes)
    return LogChunk(
        name=name,
        start=chunk.start,
        end=chunk.end,
        size=chunk.size,
        eof=chunk.end >= chunk.size,
        text=chunk.text,
    )


_MAX_ROUND_DEPTH = 6  # bound the nested-dir walk (step → round → finding → …)


def _list_artifacts(directory: Path, *, prefix: str = "") -> list[StepArtifact]:
    out: list[StepArtifact] = []
    for f in sorted(directory.iterdir()):
        # Skip symlinks: the read path is containment-checked, but a symlink need
        # never be *advertised* — list only real files the run wrote here.
        if f.is_file() and not f.is_symlink():
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            out.append(StepArtifact(name=f.name, size=size, path=prefix + f.name))
    return out


def _list_rounds(directory: Path, *, prefix: str = "", depth: int = 0) -> list["StepRound"]:
    """Nested round/sub-step dirs under ``directory``, each with its files and
    its own (recursively listed) sub-rounds. ``prefix`` is the path so far
    relative to the step dir; ``path`` on each node is the ``?artifact=`` prefix
    its files link under. Bounded depth + symlink-skipping keep the walk finite
    and contained."""
    if depth >= _MAX_ROUND_DEPTH:
        return []
    out: list[StepRound] = []
    for d in sorted(p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()):
        rel = prefix + d.name
        out.append(
            StepRound(
                name=d.name,
                path=rel,
                artifacts=_list_artifacts(d, prefix=rel + "/"),
                rounds=_list_rounds(d, prefix=rel + "/", depth=depth + 1),
            )
        )
    return out


__all__ = [
    "RunStore",
    "RunRow",
    "StepDetail",
    "StepRound",
    "StepArtifact",
    "LogChunk",
    "RunNotFound",
    "UnsafePath",
    "duration_seconds",
    "ALLOWED_LOG_NAMES",
]
