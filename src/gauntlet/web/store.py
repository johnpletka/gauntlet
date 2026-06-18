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

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import (
    RUN_ABORTED,
    RUN_DONE,
    RUN_FAILED,
    DONE,
    Manifest,
    StepRecord,
)

# Run-level states that mean a run has finished (so an `ended` is meaningful).
_TERMINAL_RUN_STATES = frozenset({RUN_DONE, RUN_ABORTED, RUN_FAILED})


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
    n_steps: int = 0
    n_done: int = 0
    warnings_count: int = 0
    updated: str | None = None


class StepArtifact(BaseModel):
    name: str
    size: int


class StepRound(BaseModel):
    """A nested round dir of an ``adversarial_cycle`` (e.g. ``r1-review``)."""

    name: str
    artifacts: list[StepArtifact] = []
    items: list[str] = []  # nested subdir names (e.g. per-finding triage dirs)


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
    """The step record `current_step` points at (any iteration), or None."""
    if not man.current_step:
        return None
    for rec in man.steps:
        if rec.id == man.current_step:
            return rec
    return None


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
    """Read-only view over a repo's runs (one ``serve`` instance = one repo)."""

    def __init__(self, repo_root: Path, config: RunConfig) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config
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
    def from_repo(cls, repo_root: Path) -> "RunStore":
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
        return cls(repo_root, config)

    # ---- layout --------------------------------------------------------------
    @property
    def run_root_dir(self) -> Path:
        return (self.repo_root / self.config.run_root).resolve()

    def _assert_contained(self, path: Path) -> Path:
        """Fail closed if ``path`` resolves outside the run root (FR-10.1)."""
        root = self.run_root_dir
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise UnsafePath(f"path escapes the run root: {path}")
        return resolved

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
            p = self._assert_contained(step_dir / cand)
            if p.exists() and p.is_file():
                path = p
                name = cand
                break
        if path is None:
            raise RunNotFound(
                f"no tailable log for step {step!r} in run {run_dir.name!r}"
            )

        size = path.stat().st_size
        start = max(0, offset)
        if start > size:  # the file shrank under us → resync from the top
            start = 0
        with path.open("rb") as fh:
            fh.seek(start)
            raw = fh.read(max_bytes)
        end = start + len(raw)
        return LogChunk(
            name=name,
            start=start,
            end=end,
            size=size,
            eof=end >= size,
            text=raw.decode("utf-8", errors="replace"),
        )

    # ---- list view (FR-1.1) --------------------------------------------------
    def _row_for(self, slug: str) -> RunRow | None:
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
            owned=False,  # no supervisor in P1 — every run is observed
            attached=False,
            n_steps=len(man.steps),
            n_done=sum(1 for s in man.steps if s.status == DONE),
            warnings_count=len(man.warnings),
            updated=_mtime_iso(manifest_path),
        )

    def list_rows(self) -> list[RunRow]:
        """One row per slug (its latest/active run), most-recently-updated first."""
        rows = [r for slug in self.slugs() if (r := self._row_for(slug)) is not None]
        rows.sort(key=lambda r: (r.updated or "", r.slug), reverse=True)
        return rows

    # ---- detail view (FR-2.1) ------------------------------------------------
    def manifest(self, slug: str, run_id: str | None = None) -> Manifest:
        run_dir = self.run_dir(slug, run_id)
        try:
            return Manifest.load(run_dir / "manifest.json")
        except (OSError, ValueError) as exc:
            raise RunNotFound(f"unreadable manifest for {slug!r}: {exc}") from exc

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
        rounds: list[StepRound] = []
        for d in sorted(p for p in step_dir.iterdir() if p.is_dir()):
            rounds.append(
                StepRound(
                    name=d.name,
                    artifacts=_list_artifacts(d),
                    items=sorted(c.name for c in d.iterdir() if c.is_dir()),
                )
            )
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


def _list_artifacts(directory: Path) -> list[StepArtifact]:
    out: list[StepArtifact] = []
    for f in sorted(directory.iterdir()):
        if f.is_file():
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            out.append(StepArtifact(name=f.name, size=size))
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
