"""P4 (live-run-observability): console live-tail wiring + freshness signal.

Two deliverables, both validated here:

* **FR-4** — the *already-built* console SSE consumer (`sse.log_tail_stream`)
  lights up over a **growing** ``events.jsonl`` with **no** new endpoint and no
  ``sse.py``/``store.py`` change: driving the generator over a file that grows
  mid-stream emits ``append`` events *before* the step completes, proving the P2
  producer → existing consumer wiring.
* **FR-5** — the advisory freshness signal. ``status``/``--json`` expose the
  **nested** ``current_step_freshness.last_event_age_s`` (age of the newest
  streamed event). The ``current_step_freshness`` **object is the nullable unit**:
  ``null`` for a non-streamed / not-applicable step or the pre-first-event window
  (events.jsonl absent or empty); a number only once ≥1 line has streamed. It is
  computed by the I/O-bearing :func:`operator.compute_current_step_freshness`
  (a single ``stat``, no event-body parse) and threaded into the pure
  :func:`operator.status_payload`. It drives no gate and no automatic action
  (FR-5.2). There is no top-level ``last_event_age_s``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gauntlet.adapters._structured import validate_schema
from gauntlet.cli import app
from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.logging.transcript import STREAM_MARKER_SUFFIX
from gauntlet.web.sse import log_tail_stream
from gauntlet.web.store import RunStore

REPO = Path(__file__).resolve().parents[2]
STATUS_SCHEMA = json.loads((REPO / "schemas" / "status.json").read_text())

runner = CliRunner()


# --- builders ----------------------------------------------------------------
def _manifest(status: str, steps: list[StepRecord], *, slug: str = "demo") -> Manifest:
    return Manifest(
        run_id="run-x",
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=status,
        steps=steps,
    )


def _step(id: str, status: str, *, type: str = "agent_task") -> StepRecord:
    return StepRecord(id=id, type=type, status=status)


def _events(
    run_instance_dir: Path, leaf: str, text: str, *, streaming: bool = True
) -> Path:
    """Write a step's events.jsonl under ``steps/<leaf>/`` and return its path.

    ``streaming`` (default) also lays down the live-stream sidecar marker that
    :meth:`StepLogger.open_stream` writes, i.e. simulates a step whose per-line
    stream is currently OPEN. Pass ``streaming=False`` to model a non-empty
    ``events.jsonl`` with NO open stream — a buffered adapter or a prior/killed
    attempt — which freshness must report as ``null`` (F-002).
    """
    step_dir = run_instance_dir / "steps" / leaf
    step_dir.mkdir(parents=True, exist_ok=True)
    path = step_dir / "events.jsonl"
    path.write_text(text)
    marker = step_dir / ("events.jsonl" + STREAM_MARKER_SUFFIX)
    if streaming:
        marker.write_text("")
    elif marker.exists():
        marker.unlink()
    return path


def _parse_sse(message: str) -> tuple[str | None, object]:
    """Parse one SSE message block → (event_name, data) or (None, comment)."""
    event = None
    data_lines: list[str] = []
    comment = None
    for line in message.splitlines():
        if line.startswith(":"):
            comment = line[1:].strip()
        elif line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    if event is None:
        return None, comment
    return event, json.loads("\n".join(data_lines))


# --- FR-4.1: the existing SSE consumer lights up over a growing file ----------
def test_log_tail_stream_emits_appends_over_a_growing_file(tmp_path: Path):
    """The P2 producer → existing consumer wiring: no new endpoint, no UI change.

    Driving ``log_tail_stream`` (verbatim, unchanged) over an ``events.jsonl``
    that grows *while the step is still running* yields ``append`` events for
    each new chunk before any completion — exactly what an in-flight streamed
    step now produces. Proves FR-4 with zero ``sse.py``/``store.py`` change.
    """
    repo = tmp_path / "repo"
    run_dir = repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    _manifest(
        M.RUN_RUNNING, [_step("impl", M.RUNNING)], slug="alpha"
    ).write_atomic(run_dir / "manifest.json")
    log = _events(run_dir, "impl", '{"t":"start"}\n')
    store = RunStore(repo, RunConfig())

    async def scenario():
        async def never() -> bool:
            return False

        gen = log_tail_stream(
            store, "alpha", "impl", start=0,
            is_disconnected=never, interval=0.0, max_iters=3,
        )
        first = await gen.__anext__()              # the line present at connect
        with log.open("a") as fh:                  # producer appends mid-stream
            fh.write('{"t":"progress"}\n')
        second = await gen.__anext__()             # only the appended bytes
        third = await gen.__anext__()              # quiet tick → keepalive
        return first, second, third

    first, second, third = asyncio.run(scenario())

    e1, d1 = _parse_sse(first)
    assert e1 == "append" and d1["text"] == '{"t":"start"}\n'
    e2, d2 = _parse_sse(second)
    assert e2 == "append" and d2["text"] == '{"t":"progress"}\n'
    # The second chunk picked up exactly where the first left off — append-only.
    assert d2["start"] == len('{"t":"start"}\n')
    name, comment = _parse_sse(third)
    assert name is None and comment == "keepalive"


# --- FR-5.1: compute_current_step_freshness (the single I/O point) ------------
def test_freshness_is_age_of_events_mtime_for_running_streamed_step(tmp_path: Path):
    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])
    path = _events(tmp_path, "s", '{"t":"start"}\n')
    # Pin a known mtime so the age is exact (no wall-clock flake).
    os.utime(path, (1_000.0, 1_000.0))
    age = op.compute_current_step_freshness(
        man, tmp_path, streaming=True, now=1_003.5
    )
    assert age == pytest.approx(3.5)


def test_freshness_none_when_not_streaming(tmp_path: Path):
    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])
    _events(tmp_path, "s", '{"t":"start"}\n')
    # Flag off ⇒ a non-streamed run is always null, even with a live file present.
    assert op.compute_current_step_freshness(man, tmp_path, streaming=False) is None


@pytest.mark.parametrize("run_status", [M.RUN_PARKED, M.RUN_DONE, M.RUN_FAILED])
def test_freshness_none_when_run_not_running(tmp_path: Path, run_status: str):
    # Only a `running` run has a streaming step; a parked/done/failed run is null.
    man = _manifest(run_status, [_step("s", M.DONE)])
    _events(tmp_path, "s", '{"t":"x"}\n')
    assert op.compute_current_step_freshness(man, tmp_path, streaming=True) is None


def test_freshness_none_in_pre_first_event_window(tmp_path: Path):
    """The 'running but no event yet' window is a single, well-defined null.

    Absent file (stat raises) and an established-but-empty file both yield null
    — never 0, never a surfaced stat error, never an age off the create time.
    Only once ≥1 line has been appended does a number land (FR-5.1).
    """
    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])

    # (a) events.jsonl absent entirely.
    assert op.compute_current_step_freshness(man, tmp_path, streaming=True) is None

    # (b) file exists but is empty (the producer truncates/establishes it first).
    path = _events(tmp_path, "s", "")
    assert path.stat().st_size == 0
    assert op.compute_current_step_freshness(man, tmp_path, streaming=True) is None

    # (c) once a complete line is appended → a number.
    path.write_text('{"t":"start"}\n')
    age = op.compute_current_step_freshness(man, tmp_path, streaming=True)
    assert isinstance(age, float) and age >= 0.0


def test_freshness_clamps_clock_skew_to_zero(tmp_path: Path):
    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])
    path = _events(tmp_path, "s", '{"t":"x"}\n')
    os.utime(path, (2_000.0, 2_000.0))
    # `now` behind the mtime (skew) must never produce a negative age.
    assert op.compute_current_step_freshness(
        man, tmp_path, streaming=True, now=1_999.0
    ) == 0.0


# --- F-002: only the CURRENT open stream is current progress ------------------
def test_freshness_null_without_open_stream_marker(tmp_path: Path):
    """A non-empty events.jsonl with NO open-stream marker is never current.

    Models the two ways a non-empty events.jsonl exists without a live stream
    for *this* invocation: a buffered (non-streamable) adapter that wrote the
    file at end-of-step, or a prior/killed attempt's leftover. Both must read as
    null until the current stream opens and appends, per the P4 contract (F-002).
    """
    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])
    # Non-empty events, but the producer has not opened a live stream (no marker).
    path = _events(tmp_path, "s", '{"t":"stale"}\n', streaming=False)
    assert path.stat().st_size > 0
    assert op.compute_current_step_freshness(man, tmp_path, streaming=True) is None

    # The current stream opens (open_stream truncates the file + writes marker),
    # so the leftover content is gone and freshness stays null until a new append.
    marker = path.parent / ("events.jsonl" + STREAM_MARKER_SUFFIX)
    marker.write_text("")
    path.write_text("")  # open_stream's truncate
    assert op.compute_current_step_freshness(man, tmp_path, streaming=True) is None

    # Only once the current stream appends a line does a number land.
    path.write_text('{"t":"fresh"}\n')
    age = op.compute_current_step_freshness(man, tmp_path, streaming=True)
    assert isinstance(age, float) and age >= 0.0


def test_open_stream_marks_then_close_clears(tmp_path: Path):
    """The producer half of F-002: open_stream writes the marker, close removes it.

    A freshly opened stream over a *stale* non-empty events.jsonl truncates it
    and marks the stream open, so freshness is null until the new append; once
    the stream closes, the marker is gone and freshness reverts to null even
    though the (now repopulated) file is non-empty.
    """
    from gauntlet.logging.redact import RedactingWriter
    from gauntlet.logging.transcript import StepLogger

    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])
    # Plant a stale non-empty events.jsonl with no marker (prior attempt).
    path = _events(tmp_path, "s", '{"t":"stale"}\n', streaming=False)
    assert op.compute_current_step_freshness(man, tmp_path, streaming=True) is None

    logger = StepLogger(RedactingWriter(), path.parent)
    stream = logger.open_stream()
    assert stream.marker_path is not None and stream.marker_path.exists()
    assert path.read_text() == ""  # the open truncated the stale content
    assert op.compute_current_step_freshness(man, tmp_path, streaming=True) is None

    stream.append_line('{"t":"fresh"}\n')
    age = op.compute_current_step_freshness(man, tmp_path, streaming=True)
    assert isinstance(age, float) and age >= 0.0

    stream.close()
    assert not stream.marker_path.exists()  # marker cleared on close
    assert path.stat().st_size > 0  # file still non-empty …
    assert op.compute_current_step_freshness(man, tmp_path, streaming=True) is None
    # … but no open stream ⇒ not current progress.


# --- F-001: an unsafe / out-of-tree step id fails closed, never stats ---------
@pytest.mark.parametrize(
    "bad_id",
    ["/tmp", "../../etc", "a/b", "..", ".", "a\x00b"],
)
def test_freshness_unsafe_step_id_raises_contract_error(tmp_path: Path, bad_id: str):
    """A corrupt manifest step id must fail closed, not stat outside the tree.

    Separators, ``.``/``..``, absolute paths, and a NUL byte are all rejected as
    a :class:`StatusContractError` BEFORE any path is built or stat'd. The NUL
    case in particular would raise an uncaught ``ValueError`` from ``stat`` under
    the old code; here it is a contract error like the rest (F-001).
    """
    man = _manifest(M.RUN_RUNNING, [_step(bad_id, M.RUNNING)])
    with pytest.raises(op.StatusContractError):
        op.compute_current_step_freshness(man, tmp_path, streaming=True)


def test_freshness_symlinked_leaf_escaping_tree_raises(tmp_path: Path):
    """A safe-named step leaf that is a symlink out of the run tree fails closed.

    The id itself is a single safe segment, so segment validation passes, but the
    resolved events.jsonl escapes the run instance — the containment check turns
    that into a :class:`StatusContractError` rather than stat'ing out-of-tree
    (F-001 defense in depth).
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "events.jsonl").write_text('{"t":"x"}\n')
    (outside / ("events.jsonl" + STREAM_MARKER_SUFFIX)).write_text("")

    run_instance = tmp_path / "inst"
    (run_instance / "steps").mkdir(parents=True)
    (run_instance / "steps" / "leaf").symlink_to(outside, target_is_directory=True)

    man = _manifest(M.RUN_RUNNING, [_step("leaf", M.RUNNING)])
    with pytest.raises(op.StatusContractError):
        op.compute_current_step_freshness(man, run_instance, streaming=True)


# --- FR-5.1: the payload object is the nullable unit, schema-valid both ways ---
def _payload(man: Manifest, *, freshness: float | None, run_root: Path) -> dict:
    rstate = op.compute_run_state(man, op.LIVENESS_ALIVE)
    driver = op.DriverInfo(op.LIVENESS_ALIVE, None, None, None)
    return op.status_payload(
        man, driver, rstate, None,
        run_root=run_root, run_instance_dir=run_root / "demo" / "run-x",
        current_step_freshness=freshness,
    )


def test_payload_carries_nested_object_when_streaming(tmp_path: Path):
    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])
    payload = _payload(man, freshness=3.2, run_root=tmp_path)
    assert payload["current_step_freshness"] == {"last_event_age_s": 3.2}
    assert "last_event_age_s" not in payload  # no top-level field
    validate_schema(payload, STATUS_SCHEMA)


def test_payload_carries_null_when_not_streaming(tmp_path: Path):
    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])
    payload = _payload(man, freshness=None, run_root=tmp_path)
    assert payload["current_step_freshness"] is None
    validate_schema(payload, STATUS_SCHEMA)


# --- FR-5.2: freshness is purely advisory — read-only, drives nothing ---------
def test_freshness_computation_mutates_nothing(tmp_path: Path):
    """A deliberately stale value triggers no manifest/state change (FR-5.2)."""
    inst = tmp_path / "demo" / "run-x"
    inst.mkdir(parents=True)
    man = _manifest(M.RUN_RUNNING, [_step("s", M.RUNNING)])
    manifest_path = inst / "manifest.json"
    man.write_atomic(manifest_path)
    before = manifest_path.read_bytes()

    path = _events(inst, "s", '{"t":"x"}\n')
    os.utime(path, (1.0, 1.0))  # ancient mtime ⇒ a large, "stale" age
    age = op.compute_current_step_freshness(
        man, inst, streaming=True, now=10_000.0
    )
    assert age and age > 9_000.0  # genuinely stale

    # Nothing was written: the manifest is byte-identical and the run is still
    # running — freshness reads, it never reconciles, halts, or recovers.
    assert manifest_path.read_bytes() == before
    assert Manifest.load(manifest_path).status == M.RUN_RUNNING


# --- FR-5.1: end-to-end through `gauntlet status --json` ----------------------
def _repo_with_run(tmp_path: Path, *, config: str, events: str | None) -> Path:
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text(config)
    run_dir = tmp_path / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    man = {
        "run_id": "run-1", "slug": "demo", "branch": "gauntlet/demo",
        "base_branch": "main", "pipeline": {"name": "p", "version": 1, "hash": "h"},
        "status": "running",
        "steps": [{"id": "s", "type": "agent_task", "status": "running"}],
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))
    (tmp_path / "runs" / "demo" / "active-run.txt").write_text("run-1\n")
    if events is not None:
        _events(run_dir, "s", events)
    return tmp_path


def test_status_json_carries_freshness_with_flag_on(tmp_path, monkeypatch):
    repo = _repo_with_run(
        tmp_path, config="stream_step_output: true\n", events='{"t":"start"}\n'
    )
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["status", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    fresh = payload["current_step_freshness"]
    assert isinstance(fresh, dict)
    assert isinstance(fresh["last_event_age_s"], (int, float))
    validate_schema(payload, STATUS_SCHEMA)


def test_status_json_null_freshness_with_flag_off(tmp_path, monkeypatch):
    # Flag off ⇒ null even though a live events.jsonl is present on disk.
    repo = _repo_with_run(tmp_path, config="{}\n", events='{"t":"start"}\n')
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["status", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["current_step_freshness"] is None
    validate_schema(payload, STATUS_SCHEMA)


def test_status_json_null_freshness_pre_first_event(tmp_path, monkeypatch):
    # Flag on but the producer has not written the first line yet (no file).
    repo = _repo_with_run(tmp_path, config="stream_step_output: true\n", events=None)
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["status", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["current_step_freshness"] is None
    validate_schema(payload, STATUS_SCHEMA)
