"""Live freshness surface (P2): SSE streams + byte-offset log tail.

Covers the two async SSE generators (`event_stream`, `log_tail_stream`) driven
directly with ``asyncio.run`` (no async test framework is configured), the sync
``RunStore.step_log`` byte-offset reader, and the HTTP wiring via ``TestClient``
(including the F-006 traversal rejection on the step-log path — the first
user-selected file path the API exposes).
"""

from __future__ import annotations

import asyncio
import functools
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.web.service import TOKEN_HEADER, create_app
from gauntlet.web.sse import event_stream, log_tail_stream
from gauntlet.web.store import RunNotFound, RunStore, UnsafePath
from gauntlet.web.watcher import Watcher

TOKEN = "test-web-token-secret"


# --- fixtures ----------------------------------------------------------------


def _manifest(slug: str, run_id: str, *, status: str, steps, current_step) -> Manifest:
    return Manifest(
        run_id=run_id,
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:dead"),
        status=status,
        current_step=current_step,
        steps=steps,
    )


def _write(run_dir: Path, man: Manifest) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    man.write_atomic(run_dir / "manifest.json")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    rd = repo / "runs" / "alpha" / "run-1"
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="running",
            steps=[StepRecord(id="impl", type="agent_task", status="running")],
            current_step="impl",
        ),
    )
    # A step dir with a tailable log.
    step_dir = rd / "steps" / "impl"
    step_dir.mkdir(parents=True)
    (step_dir / "events.jsonl").write_text('{"t":"start"}\n')
    return repo


@pytest.fixture
def store(repo: Path) -> RunStore:
    return RunStore(repo, RunConfig())


def _auth() -> dict[str, str]:
    return {TOKEN_HEADER: TOKEN}


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


# --- event_stream generator --------------------------------------------------


def test_event_stream_snapshot_then_transitions(repo: Path, store: RunStore):
    rd = repo / "runs" / "alpha" / "run-1"
    watcher = Watcher(store)
    watcher.poll_once()  # prime: register run-1 @ state A (no subscribers yet)

    async def scenario():
        async def never() -> bool:
            return False

        gen = event_stream(
            store, watcher, is_disconnected=never, keepalive=5.0, max_events=2
        )
        out = [await gen.__anext__()]  # snapshot

        # Two real transitions while the stream is parked on the queue.
        _write(
            rd,
            _manifest(
                "alpha", "run-1", status="parked",
                steps=[StepRecord(id="g", type="human_gate", status="parked")],
                current_step="g",
            ),
        )
        watcher.poll_once()
        _write(
            rd,
            _manifest(
                "alpha", "run-1", status="done",
                steps=[StepRecord(id="g", type="human_gate", status="done")],
                current_step=None,
            ),
        )
        watcher.poll_once()

        out.append(await gen.__anext__())
        out.append(await gen.__anext__())
        # max_events=2 → the generator now stops.
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        return out

    msgs = asyncio.run(scenario())
    ev0, data0 = _parse_sse(msgs[0])
    assert ev0 == "snapshot"
    assert any(r["slug"] == "alpha" for r in data0["rows"])

    ev1, data1 = _parse_sse(msgs[1])
    ev2, data2 = _parse_sse(msgs[2])
    assert ev1 == "transition" and data1["run_status"] == "parked"
    assert data1["current_step"] == "g"
    assert ev2 == "transition" and data2["run_status"] == "done"


def test_event_stream_keepalive_when_quiet(repo: Path, store: RunStore):
    watcher = Watcher(store)
    watcher.poll_once()

    async def scenario():
        async def never() -> bool:
            return False

        gen = event_stream(
            store, watcher, is_disconnected=never, keepalive=0.01, max_events=1
        )
        await gen.__anext__()  # snapshot
        # No transition published → the get() times out and a keepalive is sent.
        return await gen.__anext__()

    msg = asyncio.run(scenario())
    name, comment = _parse_sse(msg)
    assert name is None and comment == "keepalive"


def test_event_stream_stops_on_disconnect(repo: Path, store: RunStore):
    watcher = Watcher(store)
    watcher.poll_once()

    async def scenario():
        async def disconnected() -> bool:
            return True

        gen = event_stream(
            store, watcher, is_disconnected=disconnected, keepalive=5.0
        )
        await gen.__anext__()  # snapshot, then the loop sees disconnect and ends
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(scenario())
    # The subscriber was cleaned up in the generator's finally.
    assert watcher._subscribers == set()


# --- log_tail_stream generator -----------------------------------------------


def test_log_tail_stream_emits_only_appended_bytes(repo: Path, store: RunStore):
    log = repo / "runs" / "alpha" / "run-1" / "steps" / "impl" / "events.jsonl"

    async def scenario():
        async def never() -> bool:
            return False

        gen = log_tail_stream(
            store, "alpha", "impl", start=0,
            is_disconnected=never, interval=0.0, max_iters=3,
        )
        first = await gen.__anext__()       # reads the existing line
        with log.open("a") as fh:
            fh.write('{"t":"more"}\n')
        second = await gen.__anext__()      # reads only the appended line
        third = await gen.__anext__()       # nothing new → keepalive
        return first, second, third

    first, second, third = asyncio.run(scenario())
    e1, d1 = _parse_sse(first)
    assert e1 == "append" and d1["text"] == '{"t":"start"}\n'
    assert d1["start"] == 0 and d1["end"] == len('{"t":"start"}\n')

    e2, d2 = _parse_sse(second)
    assert e2 == "append" and d2["text"] == '{"t":"more"}\n'
    assert d2["start"] == len('{"t":"start"}\n')  # picked up exactly where it left off

    name, comment = _parse_sse(third)
    assert name is None and comment == "keepalive"


# --- RunStore.step_log byte-offset reader ------------------------------------


def test_step_log_from_offset(store: RunStore):
    full = store.step_log("alpha", "impl", offset=0)
    assert full.text == '{"t":"start"}\n'
    assert full.start == 0 and full.eof is True and full.size == full.end

    tail = store.step_log("alpha", "impl", offset=5)
    assert tail.text == '"start"}\n'
    assert tail.start == 5


def test_step_log_offset_past_eof_resets(store: RunStore):
    # A `from` beyond EOF (file shrank/rotated) resets the cursor to 0.
    chunk = store.step_log("alpha", "impl", offset=10_000)
    assert chunk.start == 0
    assert chunk.text == '{"t":"start"}\n'


def test_step_log_explicit_name_allowlist(repo: Path, store: RunStore):
    step_dir = repo / "runs" / "alpha" / "run-1" / "steps" / "impl"
    (step_dir / "transcript.md").write_text("# hello\n")
    chunk = store.step_log("alpha", "impl", name="transcript.md")
    assert chunk.name == "transcript.md" and chunk.text == "# hello\n"


def test_step_log_rejects_disallowed_name(store: RunStore):
    # A user-supplied name outside the allowlist must not address an arbitrary
    # file in the step dir (review F-006 containment).
    with pytest.raises(UnsafePath):
        store.step_log("alpha", "impl", name="prompt.md")


def test_step_log_rejects_traversal_name(store: RunStore):
    with pytest.raises(UnsafePath):
        store.step_log("alpha", "impl", name="../manifest.json")


def test_step_log_rejects_symlink_escape(repo: Path, store: RunStore):
    """An allowed-name symlink that escapes the step dir is rejected (review
    F-002): it stays under the run root but points outside this step's dir, so it
    must not be read as the step's log."""
    step_dir = repo / "runs" / "alpha" / "run-1" / "steps" / "impl"
    # `transcript.md` is an allowed name; point it at another file under the run
    # root but outside the step dir (the manifest).
    target = repo / "runs" / "alpha" / "run-1" / "manifest.json"
    (step_dir / "transcript.md").symlink_to(target)
    with pytest.raises(UnsafePath):
        store.step_log("alpha", "impl", name="transcript.md")


def test_step_log_traversal_step_rejected(store: RunStore):
    with pytest.raises(UnsafePath):
        store.step_log("alpha", "../../etc")


def test_step_log_missing_log_404(repo: Path, store: RunStore):
    # A step dir that exists but has no tailable log → RunNotFound (404).
    bare = repo / "runs" / "alpha" / "run-1" / "steps" / "bare"
    bare.mkdir()
    with pytest.raises(RunNotFound):
        store.step_log("alpha", "bare")


# --- HTTP wiring -------------------------------------------------------------


def test_log_endpoint_byte_offset(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    resp = client.get(
        "/api/runs/alpha/steps/impl/log", headers=_auth(), params={"from": 5}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == '"start"}\n'
    assert body["start"] == 5


def test_log_endpoint_traversal_step_rejected(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    # `%2e%2e` reaches the handler as a literal ".." step segment.
    resp = client.get("/api/runs/alpha/steps/%2e%2e/log", headers=_auth())
    assert resp.status_code == 400


def test_log_endpoint_disallowed_name_rejected(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    resp = client.get(
        "/api/runs/alpha/steps/impl/log",
        headers=_auth(),
        params={"name": "prompt.md"},
    )
    assert resp.status_code == 400


def test_log_endpoint_requires_token(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    assert client.get("/api/runs/alpha/steps/impl/log").status_code == 401


def test_events_endpoint_requires_token(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    assert client.get("/events").status_code == 401


def _sse_messages(body: str) -> list[str]:
    """Split a collected SSE response body into its message blocks (``\\n\\n``-
    separated), so a fully-read finite stream can be parsed one message at a time.

    The repo's ``starlette.testclient`` buffers the whole response before
    returning (it cannot read an *infinite* stream incrementally — a direct read
    of the live `/events`/`/log/stream` endpoints deadlocks in
    ``client.stream().__enter__``). So these HTTP-boundary tests bound the route's
    generator (``max_events`` / ``max_iters`` via ``monkeypatch``) to make the
    StreamingResponse finite, then assert over the collected body — exercising the
    real route, auth, StreamingResponse, and lifespan-started watcher.
    """
    return [blk for blk in body.split("\n\n") if blk.strip()]


def test_events_route_registered_and_streaming(
    repo: Path, store: RunStore, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end `/events` wiring (review F-004): the StreamingResponse yields
    the initial snapshot then a live transition pushed by the lifespan-started
    watcher — exercising auth, the StreamingResponse, and the watcher hookup at
    the HTTP boundary, not just the internal `event_stream` generator."""
    watcher = Watcher(store, interval=0.02)
    watcher.poll_once()  # prime running so the loop's first poll is a no-op
    # Bound the route's generator so the (otherwise infinite) stream is finite and
    # the buffering TestClient can return it: one snapshot + one transition.
    monkeypatch.setattr(
        "gauntlet.web.service.event_stream",
        functools.partial(event_stream, max_events=1),
    )
    app = create_app(store, token=TOKEN, watcher=watcher)
    paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/events" in paths
    assert app.state.watcher.store is store

    rd = repo / "runs" / "alpha" / "run-1"
    stop = threading.Event()

    def drive_transition() -> None:
        # Rewrite to a parked state until the request completes; the lifespan
        # watcher loop observes it and publishes a transition to the open stream
        # (a repeated write guarantees one lands *after* the stream subscribes,
        # regardless of TestClient/loop scheduling).
        parked = _manifest(
            "alpha", "run-1", status="parked",
            steps=[StepRecord(id="g", type="human_gate", status="parked")],
            current_step="g",
        )
        while not stop.is_set():
            _write(rd, parked)
            stop.wait(0.03)

    driver = threading.Thread(target=drive_transition, daemon=True)
    with TestClient(app) as client:  # lifespan starts the watcher poll loop
        driver.start()
        resp = client.get("/events", headers=_auth())
        stop.set()
        driver.join(timeout=2)

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    msgs = _sse_messages(resp.text)
    ev0, data0 = _parse_sse(msgs[0])
    assert ev0 == "snapshot"
    assert any(r["slug"] == "alpha" for r in data0["rows"])
    ev1, data1 = _parse_sse(msgs[1])
    assert ev1 == "transition"
    assert data1["run_status"] == "parked"
    assert data1["current_step"] == "g"


def test_log_stream_route_registered(store: RunStore, monkeypatch: pytest.MonkeyPatch):
    """End-to-end `/log/stream` wiring (review F-004): the tail StreamingResponse
    yields an `append` event carrying the step log's bytes over the HTTP
    boundary, not just the internal `log_tail_stream` generator."""
    monkeypatch.setattr(
        "gauntlet.web.service.log_tail_stream",
        functools.partial(log_tail_stream, max_iters=1, interval=0.0),
    )
    app = create_app(store, token=TOKEN)
    paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/api/runs/{slug}/steps/{step}/log/stream" in paths

    with TestClient(app) as client:
        resp = client.get(
            "/api/runs/alpha/steps/impl/log/stream", headers=_auth()
        )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    name, data = _parse_sse(_sse_messages(resp.text)[0])
    assert name == "append"
    assert data["text"] == '{"t":"start"}\n'
    assert data["start"] == 0


# --- live partials -----------------------------------------------------------


def test_partial_runs_fragment(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    resp = client.get("/partials/runs", headers=_auth())
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # A fragment, not a full page (no <html>/<body> wrapper).
    assert "<html" not in resp.text.lower()
    assert "alpha" in resp.text


def test_partial_run_detail_fragment(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    resp = client.get("/partials/runs/alpha", headers=_auth())
    assert resp.status_code == 200
    assert "<html" not in resp.text.lower()
    assert "impl" in resp.text


def test_partials_require_token(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    assert client.get("/partials/runs").status_code == 401
    assert client.get("/partials/runs/alpha").status_code == 401


def test_full_pages_wire_live_regions(store: RunStore):
    client = TestClient(create_app(store, token=TOKEN))
    list_page = client.get("/", headers=_auth()).text
    assert 'data-live-src="/partials/runs' in list_page
    assert "data-sse=" in list_page
    assert "/static/live.js" in list_page
    detail_page = client.get("/runs/alpha", headers=_auth()).text
    assert 'data-live-src="/partials/runs/alpha' in detail_page
