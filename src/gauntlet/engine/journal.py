"""Append-only authoritative state journal (P6, plan §4.6 / R8).

The journal — not ``manifest.json`` — is the authoritative record of a run's
state transitions. It lives under the ignored run-instance state dir
(``<run_dir>/journal/``, the same precedent as ``heartbeat.json`` and
``suspensions.jsonl``), so branch reset/clean/rollback never touch it: a
``git reset --hard`` that materializes an OLD committed ``manifest.json``
no longer rewinds the state machine (R8) — the next contact rebuilds the
projection from the journal head.

Design (deliberately boring, plan §2 "determinism over cleverness"):

* **One atomic event file per authoritative transition.** Every
  :meth:`~gauntlet.engine.manifest.Manifest.write_atomic` — the single
  atomic-persist primitive every write-ahead site already uses — first
  appends one event here (write-ahead: the journal is authoritative), then
  replaces the projection. The event rides the SAME logical transition the
  manifest persists today; no new kill window is introduced — a kill between
  event append and projection replace leaves the projection exactly one
  journaled state behind, which the next contact reconciles idempotently.
* **State-carrying events embed the exact projection payload.** Each
  transition event stores ``state_json`` — the verbatim serialized manifest
  the engine wrote — plus its SHA-256. A projection rebuild therefore
  reproduces ``manifest.json`` byte-for-byte by construction (no serializer
  round-trip drift), for every field the engine ever persisted.
* **Audit events** (``RecoverySnapshotCreated`` / ``RecoveryActionPlanned``
  / ``RecoveryActionApplied``) carry no state; they are appended by the
  recovery executor as evidence and deduplicated by idempotency key. The
  authoritative state chain is the state-carrying events alone.
* **Idempotent finalization (deliverable 3).** A torn/partial event file is
  quarantined deterministically (renamed ``*.torn`` — preserved as evidence,
  never deleted) on the next mutating contact; a duplicated idempotency key
  keeps the first occurrence and quarantines the rest (``*.dup``); a
  projection behind the journal head is caught up by rewriting the head
  bytes. Nothing is ever double-applied: state events are absolute
  snapshots, so replay is last-valid-state-wins.
* **Migration (deliverable 4, plan §8).** A pre-P6 run (manifest only, no
  journal) gets a deterministic ``JournalGenesis`` event embedding the
  on-disk manifest bytes verbatim on first mutating contact. Old runs stay
  loadable/classifiable exactly as before — read-only status never writes.

This module is deliberately free of every other gauntlet import (no gitops,
no manifest, no pydantic): journal/projection writes are plain file
mutations — every GIT mutation stays behind ``RecoveryExecutor`` (plan §9),
and the static checks can prove this module cannot reach a destructive git
verb. The step/run lifecycle literals used by the kind derivation are pinned
to ``manifest.py``'s constants by a unit drift guard.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

JOURNAL_DIRNAME = "journal"
JOURNAL_SCHEMA_VERSION = 1

# Event vocabulary (plan §4.6) + the deliverable-4 genesis event.
EVENT_KINDS = (
    "JournalGenesis",
    "AttemptStarted",
    "AgentCallStarted",
    "AgentCallFinished",
    "CheckpointObserved",
    "ArtifactValidationFailed",
    "DependencyUnavailable",
    "AttemptInterrupted",
    "RecoverySnapshotCreated",
    "RecoveryActionPlanned",
    "RecoveryActionApplied",
    "StepCompleted",
    "RunStatusChanged",
    # P7c worktree lifecycle (spike §10). Additive within the existing
    # extension pattern — no schema-version bump (§16) — and deliberately
    # STATE-LESS audit events (`append_audit`), never state-carrying ones:
    # the authoritative answer to "does this run have a worktree?" is
    # `git worktree list --porcelain`, which §10 makes the detection rule
    # precisely so it never depends on an event having landed. These are the
    # transition record — who adopted what, when, and at which SHA.
    "WorktreeAdopted",
    "WorktreeReleased",
)

# Lifecycle literals, pinned 1:1 to manifest.py's constants by
# tests/unit/test_journal_p6.py (drift guard) — imported nowhere so this
# module stays free of engine imports (see module docstring).
_RUNNING = "running"
_DONE = "done"
_FAILED = "failed"
_INTERRUPTED = "interrupted"
_PARKED = "parked"
_HALTED = "halted"
_SKIPPED = "skipped"
_REASON_ARTIFACT_INVALID = "artifact_invalid"
_DEPENDENCY_REASONS = frozenset(
    {"usage_limit", "usage_window", "provider_unavailable"}
)

# evt-<seq 8 digits>-<Kind>-<key12>.json — sorting lexicographically sorts by
# sequence. Quarantined files gain a ``.torn`` / ``.dup`` suffix (no longer
# ``*.json``), so they drop out of every scan while remaining evidence.
_EVENT_NAME_RE = re.compile(
    r"^evt-(?P<seq>\d{8})-(?P<kind>[A-Za-z]+)-(?P<key>[0-9a-f]{12})\.json$"
)
# Any file that *claims* a sequence number — including quarantined ones and
# leftover temp files — reserves it, so a quarantined event's seq is never
# reused (reuse would make the append-only ordering ambiguous).
_SEQ_CLAIM_RE = re.compile(r"^evt-(?P<seq>\d{8})-")


class JournalError(RuntimeError):
    """A journal invariant failed; every raise here fails closed."""


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fsync_dir(path: Path, *, strict: bool = False) -> None:
    """Flush ``path``'s directory entry.

    ``strict`` (every AUTHORITATIVE write — a state-carrying event append and
    the projection write) makes a failure LOUD: the caller's durability
    guarantee is exactly this fsync, so swallowing it would let an append
    report success while the entry is still only in the page cache — a crash
    could then lose the authority while the derived projection survives (the
    inverse of fail-closed, post-P6 review F-006). Optional evidence writes
    (audit events, quarantine renames) stay best-effort by contract: they must
    never prevent a finalization (plan §9).
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        if strict:
            raise JournalError(
                f"could not open {path} to flush the journal directory entry "
                f"({exc}); refusing to report an authoritative write durable"
            ) from exc
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if strict:
            raise JournalError(
                f"could not fsync the journal directory entry for {path} "
                f"({exc}); refusing to report an authoritative write durable"
            ) from exc
    finally:
        os.close(fd)


def _write_new_file_atomic(path: Path, text: str, *, strict: bool = True) -> None:
    """Create ``path`` atomically and exclusively (append-only discipline).

    temp + ``os.link`` (fails if the name exists — never a silent overwrite)
    + directory fsync, so a crash leaves either no file or a whole file.
    ``strict`` propagates a directory-fsync failure (see :func:`_fsync_dir`).
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".evt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    _fsync_dir(path.parent, strict=strict)


def _write_projection(path: Path, text: str) -> None:
    """Atomically (re)write the projection file (temp + fsync + replace).

    The projection is derived data (the journal is authoritative), so a
    replace — unlike the exclusive event append — is correct here. The
    directory flush is strict: a projection that is not durably in place is
    not a completed transition.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(path.parent, strict=True)


# --- best-effort observed HEAD (envelope evidence, never a decision input) ----


def _observed_head_sha(start: Path) -> str | None:
    """The containing repository's HEAD commit, read without spawning git.

    Pure-python and best-effort: this is observational envelope evidence (the
    plan §4.6 "observed branch SHA"), recorded per event but never consumed
    by a decision, so a non-repo dir (unit tests), a packed ref, or any read
    failure simply yields ``None`` rather than an error or a subprocess per
    persist.
    """
    try:
        current = start.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        git_path = candidate / ".git"
        if not git_path.exists():
            continue
        git_dir = git_path
        try:
            if git_path.is_file():  # worktree: "gitdir: <path>"
                text = git_path.read_text().strip()
                if not text.startswith("gitdir:"):
                    return None
                git_dir = (candidate / text.split(":", 1)[1].strip()).resolve()
            head = (git_dir / "HEAD").read_text().strip()
            if not head.startswith("ref:"):
                return head if re.fullmatch(r"[0-9a-f]{40}", head) else None
            ref = head.split(":", 1)[1].strip()
            ref_file = git_dir / ref
            if ref_file.exists():
                sha = ref_file.read_text().strip()
                return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None
            packed = git_dir / "packed-refs"
            if packed.exists():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + ref):
                        sha = line.split(" ", 1)[0]
                        if re.fullmatch(r"[0-9a-f]{40}", sha):
                            return sha
            return None
        except OSError:
            return None
    return None


# --- reading ------------------------------------------------------------------


def journal_dir(run_dir: Path) -> Path:
    return run_dir / JOURNAL_DIRNAME


def _event_paths(jdir: Path) -> list[Path]:
    """Valid-named event files, ascending by sequence. Missing dir → empty."""
    try:
        names = os.listdir(jdir)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [jdir / n for n in sorted(names) if _EVENT_NAME_RE.match(n)]


def _next_seq(jdir: Path) -> int:
    """1 + the highest sequence any file (incl. quarantined) claims."""
    highest = 0
    try:
        names = os.listdir(jdir)
    except (FileNotFoundError, NotADirectoryError):
        return 1
    for name in names:
        match = _SEQ_CLAIM_RE.match(name)
        if match:
            highest = max(highest, int(match.group("seq")))
    return highest + 1


def _parse_event(path: Path) -> dict | None:
    """Parse + validate one event file; ``None`` for a torn/invalid file.

    Fail closed: an event is valid only when its envelope is complete, its
    filename agrees with its body (seq, kind, key prefix), and — for a
    state-carrying event — the embedded state's hash verifies. Anything
    else is torn/foreign and must never contribute to the state chain.
    """
    match = _EVENT_NAME_RE.match(path.name)
    if not match:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    required = (
        "schema_version", "seq", "event_id", "run_id", "ts",
        "idempotency_key", "kind", "payload",
    )
    if any(key not in data for key in required):
        return None
    if data.get("kind") not in EVENT_KINDS:
        return None
    if data.get("seq") != int(match.group("seq")):
        return None
    if data.get("kind") != match.group("kind"):
        return None
    key = data.get("idempotency_key")
    if not isinstance(key, str) or not key:
        return None
    if _key12(key) != match.group("key"):
        return None
    state = data.get("state_json")
    if state is not None:
        if not isinstance(state, str):
            return None
        if data.get("state_sha256") != _sha256_text(state):
            return None
    return data


def _quarantine(path: Path, suffix: str, notes: list[str]) -> None:
    """Rename an invalid/duplicate event aside — preserved, never deleted."""
    target = path.with_name(path.name + suffix)
    try:
        if not target.exists():
            os.rename(path, target)
        else:  # extremely defensive: never clobber prior evidence
            os.rename(path, path.with_name(f"{path.name}{suffix}.{os.getpid()}"))
        _fsync_dir(path.parent)
        notes.append(f"quarantined {path.name} as {suffix.lstrip('.')} evidence")
    except OSError as exc:
        raise JournalError(
            f"could not quarantine invalid journal event {path} ({exc}); "
            "refusing to continue over an unreconciled journal"
        ) from exc


def _key12(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _dedupe(jdir: Path, *, mutate: bool, notes: list[str]) -> list[Path]:
    """Valid event paths, ascending, with duplicate keys resolved deterministically.

    Every file is parsed FIRST, then resolved (post-P6 review F-005):

    * an invalid (torn / partially written / foreign) file is quarantined as
      ``.torn`` evidence (``mutate=True``) or skipped (read-only) — it never
      wins a key and never shadows a valid retry. The earlier "keep the
      lowest sequence unparsed" rule lost BOTH copies when a torn event was
      followed by a valid retry carrying the same idempotency key: the retry
      was quarantined as a duplicate of a file that was itself then
      quarantined as torn, rolling authority back a state.
    * among the VALID events sharing a **full** idempotency key (never the
      12-hex filename digest alone — a digest collision between two distinct
      keys is not a duplicate), the earliest by sequence wins and the later
      replays are quarantined ``.dup``. State events are absolute snapshots
      and the winner is retained, so each transition applies exactly once.
    """
    kept: list[Path] = []
    winners: dict[str, Path] = {}
    for path in _event_paths(jdir):
        event = _parse_event(path)
        if event is None:
            if mutate:
                _quarantine(path, ".torn", notes)
            else:
                notes.append(f"torn journal event {path.name} (read-only skip)")
            continue
        key = event["idempotency_key"]
        if key not in winners:
            winners[key] = path
            kept.append(path)
            continue
        if mutate:
            _quarantine(path, ".dup", notes)
        else:
            notes.append(f"duplicate journal event {path.name} (read-only skip)")
    return kept


@dataclass
class _Head:
    """The newest valid state-carrying event, plus reconciliation notes."""

    event: dict | None
    path: Path | None
    notes: list[str] = field(default_factory=list)


def _head_state(jdir: Path, *, mutate: bool) -> _Head:
    """Walk backwards to the newest valid state-carrying event.

    :func:`_dedupe` has already parsed, quarantined the invalid, and
    key-resolved everything it returns, so this walk sees only valid events
    and simply skips the state-less audit ones. State events are absolute
    snapshots, so a torn or quarantined newer event can never corrupt the
    chain — the newest surviving state event is authoritative.
    """
    notes: list[str] = []
    for path in reversed(_dedupe(jdir, mutate=mutate, notes=notes)):
        event = _parse_event(path)
        if event is not None and event.get("state_json") is not None:
            return _Head(event=event, path=path, notes=notes)
    return _Head(event=None, path=None, notes=notes)


def read_events(run_dir: Path) -> list[dict]:
    """All valid events, ascending. Read-only (torn/dup files are skipped)."""
    notes: list[str] = []
    out = []
    for path in _dedupe(journal_dir(run_dir), mutate=False, notes=notes):
        event = _parse_event(path)
        if event is not None:
            out.append(event)
    return out


def evidence_fingerprint(projection_path: Path) -> str:
    """The rebuild-precondition witness for the on-disk projection.

    ``sha256:...`` of the current bytes, or the literal ``"absent"`` when the
    file is missing — the value :class:`RebuildProjectionAction` carries and
    the executor re-verifies under the lock before mutating anything.
    """
    try:
        return _sha256_text(projection_path.read_text())
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError:
        return "unreadable"


# --- appending ----------------------------------------------------------------


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A candidate state's validator: does ``text`` deserialize to a state this
# engine can actually load? Callers inject the FULL model check
# (``manifest.validate_projection_text``); the module's own default is the
# structural JSON check, which keeps journal.py standalone and engine-import
# free (plan §9) while every production caller validates against the real
# model. Post-P6 review F-002: without the model check, JSON-valid but
# schema-invalid bytes could become the authoritative head — permanently
# unloadable, and therefore a wedged run with nothing valid to rebuild from.
Validator = Callable[[str], bool]


def _default_validate(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _valid_state(text: str, validate: "Validator | None") -> bool:
    if not _default_validate(text):
        return False
    return True if validate is None else bool(validate(text))


def _envelope_context(state: dict) -> tuple[str | None, Any, str | None]:
    """(step, iteration, attempt_id) for the manifest's current step, if any."""
    step_id = state.get("current_step")
    if not step_id:
        return None, None, None
    record = None
    for rec in state.get("steps") or []:
        if isinstance(rec, dict) and rec.get("id") == step_id:
            record = rec  # last matching record = the active attempt's row
    if record is None:
        return step_id, None, None
    attempts = record.get("attempts")
    attempt = f"{step_id}#{attempts}" if isinstance(attempts, int) else None
    return step_id, record.get("iteration"), attempt


def _append_event(
    jdir: Path,
    *,
    kind: str,
    run_id: str,
    idempotency_key: str,
    payload: dict,
    step: str | None = None,
    iteration: Any = None,
    attempt_id: str | None = None,
    state_json: str | None = None,
    clock: Callable[[], str] | None = None,
    seq: int | None = None,
    strict: bool = True,
) -> dict:
    if kind not in EVENT_KINDS:
        raise JournalError(f"unknown journal event kind {kind!r}")
    jdir.mkdir(parents=True, exist_ok=True)
    clock = clock or _default_clock
    while True:
        use_seq = seq if seq is not None else _next_seq(jdir)
        event: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "seq": use_seq,
            "event_id": hashlib.sha256(
                f"{run_id}:{use_seq}:{kind}:{idempotency_key}".encode()
            ).hexdigest()[:16],
            "run_id": run_id,
            "step": step,
            "iteration": iteration,
            "attempt_id": attempt_id,
            "ts": clock(),
            "observed_branch_sha": _observed_head_sha(jdir),
            "idempotency_key": idempotency_key,
            "kind": kind,
            "payload": payload,
        }
        if state_json is not None:
            event["state_json"] = state_json
            event["state_sha256"] = _sha256_text(state_json)
        name = f"evt-{use_seq:08d}-{kind}-{_key12(idempotency_key)}.json"
        try:
            _write_new_file_atomic(
                jdir / name,
                json.dumps(event, sort_keys=True, indent=1),
                strict=strict,
            )
            return event
        except FileExistsError:
            if seq is not None:
                raise JournalError(
                    f"journal event seq {seq} already exists in {jdir}"
                ) from None
            continue  # a concurrent claim took this seq; re-derive and retry


def append_audit(
    run_dir: Path,
    kind: str,
    payload: dict,
    *,
    run_id: str,
    idempotency_key: str,
    clock: Callable[[], str] | None = None,
) -> bool:
    """Append a state-less audit event, deduplicated by idempotency key.

    Best-effort by contract: audit events are recovery evidence, never a
    gate — the durable authority for a recovery transaction is its intent
    file + snapshot ref (recovery_exec), and the authoritative state chain
    is the state-carrying events. A replayed transaction re-appending the
    same key is skipped (exactly-once in the journal); an I/O failure is
    swallowed (plan §9: optional evidence gathering must never prevent
    finalization). Returns whether an event was appended.
    """
    jdir = journal_dir(run_dir)
    key12 = _key12(idempotency_key)
    try:
        for path in _event_paths(jdir):
            if _EVENT_NAME_RE.match(path.name).group("key") == key12:
                existing = _parse_event(path)
                if (
                    existing is not None
                    and existing["idempotency_key"] == idempotency_key
                ):
                    return False  # already recorded — never double-applied
        _append_event(
            jdir,
            kind=kind,
            run_id=run_id,
            idempotency_key=idempotency_key,
            payload=payload,
            clock=clock,
            strict=False,  # optional evidence: never block a finalization
        )
        return True
    except (OSError, JournalError):
        return False


# --- the write_atomic hook (one durable transition per outcome) ---------------


def record_transition(
    manifest_path: Path,
    payload: str,
    *,
    clock: Callable[[], str] | None = None,
    validate: "Validator | None" = None,
) -> None:
    """Journal one manifest persist, write-ahead of the projection replace.

    Called by :meth:`Manifest.write_atomic` with the exact serialized payload
    it is about to write, BEFORE the projection file is replaced — so the
    journal (the authority) always leads and a kill between the two leaves a
    projection exactly one journaled state behind (reconciled idempotently
    by the next contact; no new kill window, no unclassifiable state).

    A persist whose payload is byte-identical to the journal head is not a
    transition: nothing is appended (the projection rewrite is harmless).
    A pre-P6 run dir (existing manifest, empty journal) gets its migration
    genesis first, embedding the on-disk bytes verbatim (deliverable 4).
    """
    run_dir = manifest_path.parent
    jdir = journal_dir(run_dir)
    head = _head_state(jdir, mutate=True)
    if head.event is None:
        genesis = ensure_genesis(run_dir, clock=clock, validate=validate)
        if genesis is not None:
            head = _Head(event=genesis, path=None)
    if (
        head.event is not None
        and head.event.get("state_sha256") == _sha256_text(payload)
    ):
        return  # same state re-persisted — not a transition
    try:
        state = json.loads(payload)
    except ValueError as exc:  # write_atomic serialized it; cannot happen
        raise JournalError(f"unserializable manifest payload: {exc}") from exc
    prev_state = None
    if head.event is not None and head.event.get("state_json") is not None:
        try:
            prev_state = json.loads(head.event["state_json"])
        except ValueError:
            prev_state = None  # genesis may embed pre-P6 corrupt bytes
    if not isinstance(prev_state, dict):
        prev_state = None
    kind, changes = derive_kind(prev_state, state)
    step, iteration, attempt = _envelope_context(state)
    prev_event_id = head.event["event_id"] if head.event is not None else ""
    _append_event(
        jdir,
        kind=kind,
        run_id=str(state.get("run_id") or "unknown"),
        idempotency_key=_sha256_text(
            f"transition:{prev_event_id}:{kind}:{_sha256_text(payload)}"
        ),
        payload={"changes": changes},
        step=step,
        iteration=iteration,
        attempt_id=attempt,
        state_json=payload,
        clock=clock,
    )


# --- kind derivation (plan §4.6 vocabulary over one persisted transition) -----

# Precedence when one persist carries several sub-transitions (e.g. the P4
# one-write terminalization lands a step outcome AND the mapped run status):
# the most consequential classification names the event; every detected
# sub-transition is preserved in the payload's ``changes`` list.
_KIND_RANK = {
    "StepCompleted": 0,
    "AttemptInterrupted": 1,
    "ArtifactValidationFailed": 2,
    "DependencyUnavailable": 3,
    "AgentCallFinished": 4,
    "CheckpointObserved": 5,
    "AttemptStarted": 6,
    "AgentCallStarted": 6,
    "RunStatusChanged": 7,
}


def derive_kind(prev: dict | None, cur: dict) -> tuple[str, list[str]]:
    """Classify one persisted transition into the §4.6 event vocabulary.

    Pure and total over (previous state, new state): the journal rides the
    exact transitions the engine already persists, so the kind is derived
    from the observable state delta — never from a side channel a call site
    could forget to thread through. Any residual change (warnings, human
    responses, usage accumulation) classifies as ``RunStatusChanged`` with
    the exact changed keys named in ``changes``.
    """
    changes: list[str] = []
    best: tuple[int, str] | None = None

    def consider(kind: str) -> None:
        nonlocal best
        rank = _KIND_RANK[kind]
        if best is None or rank < best[0]:
            best = (rank, kind)

    prev_steps: dict[tuple, dict] = {}
    for rec in (prev or {}).get("steps") or []:
        if isinstance(rec, dict):
            prev_steps[(rec.get("id"), rec.get("iteration"))] = rec
    for rec in cur.get("steps") or []:
        if not isinstance(rec, dict):
            continue
        old = prev_steps.get((rec.get("id"), rec.get("iteration")))
        old_status = old.get("status") if old else None
        new_status = rec.get("status")
        iteration = rec.get("iteration")
        label = f"{rec.get('id')}" + (
            f"[{iteration}]" if iteration is not None else ""
        )
        if old_status != new_status:
            changes.append(
                f"step {label}: {old_status or '(new)'} -> {new_status}"
            )
            if new_status in (_DONE, _SKIPPED):
                consider("StepCompleted")
            elif new_status == _INTERRUPTED:
                consider("AttemptInterrupted")
            elif new_status == _PARKED:
                reason = rec.get("parked_reason")
                if reason == _REASON_ARTIFACT_INVALID:
                    consider("ArtifactValidationFailed")
                elif reason in _DEPENDENCY_REASONS:
                    consider("DependencyUnavailable")
                else:  # response / gate parks: the call reached a human
                    consider("AgentCallFinished")
            elif new_status in (_FAILED, _HALTED):
                consider("AgentCallFinished")
            elif new_status == _RUNNING:
                # First entry of the attempt vs a re-entry (a resumed call
                # within the same attempt): the prior record's ``started``
                # stamp is the discriminator.
                if old is not None and old.get("started"):
                    consider("AgentCallStarted")
                else:
                    consider("AttemptStarted")
        old_checkpoints = len((old or {}).get("checkpoints") or [])
        new_checkpoints = len(rec.get("checkpoints") or [])
        if old is not None and new_checkpoints > old_checkpoints:
            last = (rec.get("checkpoints") or [])[-1]
            if isinstance(last, dict):
                changes.append(
                    f"step {label}: checkpoint r{last.get('round')}-"
                    f"{last.get('sub_step')}"
                )
            consider("CheckpointObserved")

    prev_commits = len((prev or {}).get("commits") or [])
    cur_commits = len(cur.get("commits") or [])
    if cur_commits > prev_commits:
        for entry in (cur.get("commits") or [])[prev_commits:]:
            if isinstance(entry, dict):
                changes.append(
                    f"commit recorded: {entry.get('phase')} "
                    f"{str(entry.get('sha'))[:10]}"
                )
        consider("CheckpointObserved")

    prev_status = (prev or {}).get("status")
    if prev_status != cur.get("status"):
        changes.append(f"run: {prev_status or '(new)'} -> {cur.get('status')}")
        consider("RunStatusChanged")

    if best is None:
        changed_keys = sorted(
            key
            for key in set(cur) | set(prev or {})
            if (prev or {}).get(key) != cur.get(key)
        )
        changes.append(f"state recorded; changed keys: {changed_keys}")
        return "RunStatusChanged", changes
    return best[1], changes


# --- migration genesis (deliverable 4, plan §8) -------------------------------


def ensure_genesis(
    run_dir: Path,
    *,
    clock: Callable[[], str] | None = None,
    validate: "Validator | None" = None,
) -> dict | None:
    """Migrate a pre-P6 run: one deterministic genesis event on first contact.

    When the journal holds NO state event and ``manifest.json`` holds a state
    this engine can actually LOAD, its bytes are embedded verbatim as a
    ``JournalGenesis`` state event — same input bytes ⇒ same event bytes,
    modulo the injected clock and the observed HEAD. Nothing is rewritten:
    approved artifacts, run history, and the manifest itself are untouched
    (read-only status never calls this). Returns the appended event, or
    ``None`` when there is nothing to migrate.

    A manifest that is unparseable OR schema-invalid seeds NOTHING (post-P6
    review F-002): a head the model cannot load would wedge the run — every
    later load fails and the "rebuild" would restore the invalid bytes over
    any hand repair. Such a run keeps its exact pre-P6 behavior (the load
    raises, as it did before), and the first contact after a repair seeds the
    genesis from the repaired bytes.
    """
    manifest_path = run_dir / "manifest.json"
    jdir = journal_dir(run_dir)
    if _head_state(jdir, mutate=False).event is not None:
        return None
    try:
        text = manifest_path.read_text()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not _valid_state(text, validate):
        return None
    state = json.loads(text)
    run_id = str(state.get("run_id") or "unknown")
    step, iteration, attempt = _envelope_context(state)
    return _append_event(
        jdir,
        kind="JournalGenesis",
        run_id=run_id,
        idempotency_key=_sha256_text(f"genesis:{run_id}:{_sha256_text(text)}"),
        payload={
            "migrated_from": "manifest.json",
            "note": (
                "pre-P6 run migrated on first mutating contact (plan §8): the "
                "on-disk manifest bytes are embedded verbatim; legacy attempt "
                "identity stays derived as <step_id>#<attempts>"
            ),
        },
        step=step,
        iteration=iteration,
        attempt_id=attempt,
        state_json=text,
        clock=clock,
    )


# --- reconciliation (mutating verbs) and read-only projection status ----------

HEALTH_OK = "ok"
HEALTH_NO_JOURNAL = "no_journal"
HEALTH_GENESIS = "genesis"
HEALTH_CAUGHT_UP = "caught_up"
HEALTH_RESTORED = "restored"
HEALTH_STALE = "stale"
HEALTH_UNJOURNALED = "unjournaled"
HEALTH_CORRUPT = "corrupt"
HEALTH_MISSING = "missing"


@dataclass
class ProjectionStatus:
    """One read of journal-vs-projection agreement (shared by both surfaces).

    ``health`` for the read-only surface: ``ok`` / ``no_journal`` /
    ``stale`` (projection matches an OLDER journaled state — branch reset or
    kill window) / ``unjournaled`` (a loadable projection the journal has
    never seen — an out-of-band write: a branch reset onto a pre-genesis
    committed manifest, a stale driver, or a hand-edit) / ``corrupt``
    (unloadable bytes) / ``missing``. The mutating
    :func:`reconcile_projection` returns the resolved counterparts
    (``genesis`` / ``caught_up`` / ``restored``) or ``rebuild_required``.
    """

    health: str
    run_id: str | None
    head_seq: int | None
    head_state_json: str | None
    evidence_fingerprint: str
    notes: list[str] = field(default_factory=list)


def projection_status(
    run_dir: Path, *, mutate: bool = False, validate: "Validator | None" = None
) -> ProjectionStatus:
    """Compare the on-disk projection against the journal head.

    ``mutate=False`` (status, R4's read-only half) performs no quarantine,
    no genesis, no writes — it only reports. ``mutate=True`` quarantines
    torn/duplicate events while locating the head (the deterministic
    deliverable-3 reconciliation), but still writes no projection — that is
    :func:`reconcile_projection`'s job.

    ``validate`` is the injected full-model state check (post-P6 review
    F-002): bytes that JSON-decode but the engine cannot LOAD are ``corrupt``
    — not a candidate state — so both surfaces route them to the rebuild
    action instead of ever treating them as authority.
    """
    manifest_path = run_dir / "manifest.json"
    jdir = journal_dir(run_dir)
    head = _head_state(jdir, mutate=mutate)
    notes = list(head.notes)
    fingerprint = evidence_fingerprint(manifest_path)

    try:
        on_disk: str | None = manifest_path.read_text()
    except (FileNotFoundError, NotADirectoryError):
        on_disk = None
    except OSError as exc:
        notes.append(f"projection unreadable: {exc}")
        on_disk = None

    if head.event is None:
        return ProjectionStatus(
            health=HEALTH_NO_JOURNAL,
            run_id=None,
            head_seq=None,
            head_state_json=None,
            evidence_fingerprint=fingerprint,
            notes=notes,
        )

    head_seq = head.event["seq"]
    head_state = head.event["state_json"]
    head_run_id = head.event["run_id"]
    if on_disk is None:
        health = HEALTH_MISSING
    elif on_disk == head_state:
        health = HEALTH_OK
    else:
        # A candidate state must be LOADABLE by the engine (injected model
        # check, review F-002) and belong to THIS run; anything else is
        # corrupt bytes to be rebuilt over, never a state to reason about.
        loadable = _valid_state(on_disk, validate)
        run_id_matches = False
        if loadable:
            run_id_matches = (
                str(json.loads(on_disk).get("run_id")) == head_run_id
            )
        if not loadable or not run_id_matches:
            health = HEALTH_CORRUPT
        else:
            on_disk_sha = _sha256_text(on_disk)
            matched_older = False
            for path in reversed(_dedupe(jdir, mutate=False, notes=[])):
                event = _parse_event(path)
                if event is None or event.get("state_json") is None:
                    continue
                if event.get("state_sha256") == on_disk_sha:
                    matched_older = True
                    notes.append(
                        f"projection matches journal seq {event['seq']} "
                        f"(head is seq {head_seq})"
                    )
                    break
            health = HEALTH_STALE if matched_older else HEALTH_UNJOURNALED
    return ProjectionStatus(
        health=health,
        run_id=head_run_id,
        head_seq=head_seq,
        head_state_json=head_state,
        evidence_fingerprint=fingerprint,
        notes=notes,
    )


@dataclass
class ReconcileOutcome:
    """What :func:`reconcile_projection` did (or could not do)."""

    health: str  # ok | no_journal | genesis | caught_up | restored | rebuild_required
    run_id: str | None
    head_seq: int | None
    evidence_fingerprint: str
    notes: list[str] = field(default_factory=list)
    # Where an out-of-band projection was preserved before it was replaced
    # (``restored``); ``None`` for every other outcome.
    preserved_as: str | None = None

    @property
    def rebuild_required(self) -> bool:
        return self.health == "rebuild_required"


def unjournaled_preserved_name(head_seq: int | None, text: str) -> str:
    """Deterministic evidence filename for a preserved out-of-band projection.

    Keyed on the head sequence it diverged from plus the content digest, so
    repeating the reconciliation preserves the same bytes to the same path
    (idempotent — never a growing pile of near-identical copies).
    """
    digest = _sha256_text(text).split(":", 1)[1][:12]
    return f"manifest.unjournaled-{head_seq or 0:08d}-{digest}.json"


def reconcile_projection(
    run_dir: Path,
    *,
    clock: Callable[[], str] | None = None,
    validate: "Validator | None" = None,
) -> ReconcileOutcome:
    """Idempotently reconcile journal and projection on a mutating contact.

    The deliverable-3 finalization point, mirroring the executor-intent
    replay discipline. The journal is the AUTHORITY; ``manifest.json`` is its
    projection, and no path here ever lets the projection redefine authority
    (post-P6 review F-001):

    * torn/duplicate events are quarantined by idempotency key;
    * a pre-P6 run gets its genesis (from a LOADABLE manifest only, F-002);
    * a projection matching an OLDER journaled state (a kill window, or a
      branch reset that materialized an old committed manifest — R8) is
      caught up by rewriting the exact head bytes. Nothing is lost: those
      bytes are themselves a journaled state;
    * a projection the journal has NEVER seen — an out-of-band write: a
      branch reset onto a **pre-genesis** committed manifest (the migrated-run
      shape), a stale driver, or a hand-edit — is **preserved as durable
      evidence and then replaced** from the head. Adopting it instead (the
      pre-review behavior) let exactly the reset R8 forbids redefine the
      authoritative state, and could not tell that reset apart from a
      sanctioned edit. Preserve-then-restore is lossless (the bytes stay on
      disk under a deterministic name, and every journaled state remains in
      the journal), loud, and never wedging (R1/R2) — while manifest.json
      stays what plan §5.5 says it is: never the repair mechanism;
    * a missing/corrupt projection is NOT repaired here — that is
      :class:`RebuildProjectionAction`'s job, under the executor's
      transaction ordering with the malformed original preserved first.
    """
    status = projection_status(run_dir, mutate=True, validate=validate)
    notes = list(status.notes)
    manifest_path = run_dir / "manifest.json"

    if status.health == HEALTH_NO_JOURNAL:
        genesis = ensure_genesis(run_dir, clock=clock, validate=validate)
        if genesis is not None:
            notes.append(
                f"journal genesis appended from the existing manifest "
                f"(seq {genesis['seq']}; pre-P6 migration, plan §8)"
            )
            return ReconcileOutcome(
                health=HEALTH_GENESIS,
                run_id=genesis["run_id"],
                head_seq=genesis["seq"],
                evidence_fingerprint=status.evidence_fingerprint,
                notes=notes,
            )
        return ReconcileOutcome(  # fresh run (no manifest yet): nothing to do
            health=HEALTH_NO_JOURNAL,
            run_id=None,
            head_seq=None,
            evidence_fingerprint=status.evidence_fingerprint,
            notes=notes,
        )

    if status.health == HEALTH_OK:
        return ReconcileOutcome(
            health=HEALTH_OK,
            run_id=status.run_id,
            head_seq=status.head_seq,
            evidence_fingerprint=status.evidence_fingerprint,
            notes=notes,
        )

    if status.health == HEALTH_STALE:
        _write_projection(manifest_path, status.head_state_json)
        notes.append(
            "projection catch-up: manifest.json held an older journaled "
            f"state (kill window or branch reset, R8); rewrote it from the "
            f"journal head (seq {status.head_seq})"
        )
        return ReconcileOutcome(
            health=HEALTH_CAUGHT_UP,
            run_id=status.run_id,
            head_seq=status.head_seq,
            evidence_fingerprint=evidence_fingerprint(manifest_path),
            notes=notes,
        )

    if status.health == HEALTH_UNJOURNALED:
        # An out-of-band projection: PRESERVE it, then restore authority
        # from the journal head (R2 before R8 — preserve before mutation).
        # It is never adopted: an unjournaled state is indistinguishable
        # from the branch reset R8 forbids from rewinding the state machine
        # (a migrated run resets onto a PRE-genesis committed manifest, which
        # no journal event can match), so treating it as the newest authority
        # would reintroduce exactly the loss this phase removes.
        text = manifest_path.read_text()
        preserved = manifest_path.with_name(
            unjournaled_preserved_name(status.head_seq, text)
        )
        if not preserved.exists():  # idempotent: same bytes -> same name
            _write_projection(preserved, text)
        _write_projection(manifest_path, status.head_state_json)
        notes.append(
            "projection restore: manifest.json held a state the journal has "
            "never recorded (an out-of-band write — a branch reset onto a "
            "pre-journal committed manifest, a stale driver, or a hand-edit). "
            f"Those bytes are preserved verbatim as {preserved.name} and the "
            f"authoritative journal head (seq {status.head_seq}) was restored "
            "— nothing was discarded, and manifest.json is never the repair "
            "mechanism (plan §5.5/R2/R8). To make that state authoritative, "
            "drive it through the engine's own verbs; to inspect what "
            "differed, read the preserved copy"
        )
        return ReconcileOutcome(
            health=HEALTH_RESTORED,
            run_id=status.run_id,
            head_seq=status.head_seq,
            evidence_fingerprint=evidence_fingerprint(manifest_path),
            notes=notes,
            preserved_as=preserved.name,
        )

    # corrupt / missing: rebuild through the executor action (plan §5.5).
    notes.append(
        f"projection {status.health}: rebuild from the journal head "
        f"(seq {status.head_seq}) is required; the malformed original is "
        "preserved as evidence by the executor before any rewrite"
    )
    return ReconcileOutcome(
        health="rebuild_required",
        run_id=status.run_id,
        head_seq=status.head_seq,
        evidence_fingerprint=status.evidence_fingerprint,
        notes=notes,
    )


def rebuild_source(run_dir: Path) -> tuple[str, int, str]:
    """(head state bytes, seq, event id) the projection rebuild writes.

    Read-only; raises :class:`JournalError` when the journal holds no state
    event (nothing to rebuild from — a pre-P6 run with a corrupt manifest is
    exactly as unrecoverable as it was pre-P6, and says so).
    """
    head = _head_state(journal_dir(run_dir), mutate=False)
    if head.event is None:
        raise JournalError(
            f"no state-carrying journal event under {journal_dir(run_dir)}; "
            "the projection cannot be rebuilt (pre-P6 run or empty journal)"
        )
    return head.event["state_json"], head.event["seq"], head.event["event_id"]


def write_projection_from_head(run_dir: Path) -> tuple[int, str]:
    """Rewrite manifest.json from the journal head; returns (seq, event id).

    The executor's apply step for :class:`RebuildProjectionAction` — a single
    atomic file replace, idempotent by construction (re-running converges on
    the same bytes). Evidence preservation and precondition checks belong to
    the executor, not here.
    """
    text, seq, event_id = rebuild_source(run_dir)
    _write_projection(run_dir / "manifest.json", text)
    return seq, event_id


__all__ = [
    "EVENT_KINDS",
    "JOURNAL_DIRNAME",
    "JOURNAL_SCHEMA_VERSION",
    "HEALTH_CAUGHT_UP",
    "HEALTH_CORRUPT",
    "HEALTH_GENESIS",
    "HEALTH_MISSING",
    "HEALTH_NO_JOURNAL",
    "HEALTH_OK",
    "HEALTH_RESTORED",
    "HEALTH_STALE",
    "HEALTH_UNJOURNALED",
    "JournalError",
    "ProjectionStatus",
    "ReconcileOutcome",
    "Validator",
    "unjournaled_preserved_name",
    "append_audit",
    "derive_kind",
    "ensure_genesis",
    "evidence_fingerprint",
    "journal_dir",
    "projection_status",
    "read_events",
    "rebuild_source",
    "reconcile_projection",
    "record_transition",
    "write_projection_from_head",
]
