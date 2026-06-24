"""P5 — builder resume logic + `resume-disposition` schema (FR-3/FR-5/FR-10).

The builder classifies a `--response` deterministically (FR-3.0 precedence) and
emits a machine-checkable `disposition`; the engine maps that structured outcome
to the step status. These tests drive a resume through the orchestrator with a
**scripted adapter** that returns canned disposition objects — assertions are on
structured fields and the resulting orchestration status, never on prose
substrings (FR-10 test protocol). They also assert the schema is bound
invocation-locally (the approved pipeline definition is never mutated, FR-4.1).

The conflict-park setup and the run/repo harness are reused from
``test_resume_response`` (same deterministic fixtures); only the resume adapter
differs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapters._structured import validate_schema
from gauntlet.adapters.base import AdapterCapabilities, AgentResult
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.run import RunManager

from test_resume_response import (
    CONFLICT_TEXT,
    PIPELINE,
    PIPELINE_SOLO,
    _build_repo,
    _clock,
    _drive_to_conflict,
    _run_dir,
)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "resume-disposition.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _disposition(
    disposition: str,
    *,
    responses=("implement-resp-1",),
    summary: str = "still blocked",
    requested_input: str = "the missing detail",
    artifact: str | None = None,
) -> dict:
    """Build a schema-valid disposition object for the scripted adapter."""
    obj: dict = {
        "disposition": disposition,
        "responses_considered": list(responses),
        "action_summary": f"{disposition} action",
    }
    if disposition in ("amendment_required", "new_conflict"):
        obj["conflict"] = {
            "summary": summary,
            "requested_input": requested_input,
            "artifact": artifact,
        }
    return obj


class DispositionAdapter:
    """A resumed builder that returns a canned structured `disposition` (FR-10).

    ``write`` controls whether it touches the worktree (a real proceed implements
    code; a re-park must land nothing). ``structured`` is returned verbatim as the
    adapter's structured output — set it to ``None`` to model a malformed resume
    that produces no disposition (fail-closed path).
    """

    name = "scripted"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def __init__(self, structured: dict | None, *, write: bool = False) -> None:
        self.structured = structured
        self.write = write
        self.prompts: list[str] = []
        self.schemas: list[dict | None] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self.write:
            (Path(cwd) / "feature.py").write_text("implemented\n")
        return AgentResult(
            text="resume disposition emitted",
            structured=self.structured,
            session_id="s",
            exit_code=0,
        )


class ConflictAdapter:
    """First-conflict builder that records the schema it was handed.

    Used where a test must assert the FIRST (pre-response) invocation carries NO
    schema — the plain ``ScriptedAdapter`` does not record schemas.
    """

    name = "scripted"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.schemas: list[dict | None] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return AgentResult(text=CONFLICT_TEXT, session_id="s", exit_code=0)


# --- schema validity (FR-10 oracle) -----------------------------------------
def test_proceed_dispositions_validate_without_conflict():
    schema = _schema()
    for d in ("proceed_in_place", "proceed_with_deviation"):
        validate_schema(_disposition(d), schema)  # no raise


def test_repark_dispositions_validate_with_conflict():
    schema = _schema()
    validate_schema(_disposition("new_conflict"), schema)
    validate_schema(_disposition("amendment_required", artifact="plan FR-4"), schema)


def test_conflict_required_when_reparking():
    schema = _schema()
    bad = {
        "disposition": "amendment_required",
        "responses_considered": ["implement-resp-1"],
        "action_summary": "x",
    }
    with pytest.raises(ValueError):
        validate_schema(bad, schema)


def test_conflict_forbidden_when_proceeding():
    schema = _schema()
    bad = {
        "disposition": "proceed_in_place",
        "responses_considered": ["implement-resp-1"],
        "action_summary": "x",
        "conflict": {"summary": "s", "requested_input": "q", "artifact": None},
    }
    with pytest.raises(ValueError):
        validate_schema(bad, schema)


def test_unknown_disposition_rejected():
    with pytest.raises(ValueError):
        validate_schema(
            {"disposition": "ship_it", "responses_considered": [], "action_summary": "x"},
            _schema(),
        )


# --- engine outcome mapping (FR-3/FR-5): assert orchestration status ---------
def test_proceed_in_place_completes_and_commits(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    adapter = DispositionAdapter(_disposition("proceed_in_place"), write=True)
    status = mgr.resume(
        "demo", response="Ratify option 1; no contradiction remains.",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.DONE
    assert rec.parked_reason is None  # proceed clears the discriminator
    assert rec.attempts == 0  # not a failure
    assert gitops.commit_subject(repo, "HEAD") == "P1: implement phase"


def test_proceed_with_deviation_completes(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    adapter = DispositionAdapter(_disposition("proceed_with_deviation"), write=True)
    status = mgr.resume(
        "demo", response="Proceed with option 1; defer the rest to FUTURE.md.",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.DONE
    assert rec.parked_reason is None


def test_amendment_required_reparks_no_implementation(tmp_path):
    # FR-3(b): a response that contradicts / proceeds-despite an approved artifact
    # re-parks for the FR-10.4 gate; NO implementation lands.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    disp = _disposition("amendment_required", artifact="plan FR-4")
    adapter = DispositionAdapter(disp, write=False)
    status = mgr.resume(
        "demo", response="Proceed even though this contradicts plan FR-4.",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_UPSTREAM_CONFLICT
    assert rec.attempts == 0  # a re-park is not a failure (FR-6)
    assert not (repo / "feature.py").exists()  # nothing implemented
    # the response is consumed (terminal outcome), audit trail intact
    assert rec.human_responses[-1].state == M.RESPONSE_CONSUMED


def test_amendment_required_lands_no_commit(tmp_path):
    # With a commit step in the pipeline, an amendment_required re-park must halt
    # BEFORE the commit — no P1 phase commit reaches history.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE)
    _drive_to_conflict(repo, mgr, PIPELINE)
    adapter = DispositionAdapter(
        _disposition("amendment_required", artifact="plan FR-4"), write=False
    )
    status = mgr.resume(
        "demo", response="Rewrite plan FR-4 to infer asset_root.",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    subjects = gitops._run(repo, "log", "--format=%s").splitlines()
    assert "P1: implement phase" not in subjects


def test_new_conflict_reparks_with_requested_input(tmp_path):
    # FR-5: an ambiguous response re-parks; the structured conflict.requested_input
    # names what the supplied response did NOT provide (asserted on the structured
    # field, not prose).
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    disp = _disposition(
        "new_conflict", requested_input="which of option 1 or 2 to take"
    )
    adapter = DispositionAdapter(disp, write=False)
    status = mgr.resume(
        "demo", response="do the right thing", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_UPSTREAM_CONFLICT
    assert disp["conflict"]["requested_input"] == "which of option 1 or 2 to take"
    assert disp["responses_considered"] == ["implement-resp-1"]


def test_malformed_disposition_fails_closed(tmp_path):
    # Fail closed (CLAUDE.md §2): a resume that emits no parseable disposition is a
    # genuine failure (counts one attempt), never a silent success.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = DispositionAdapter(None, write=False)  # no structured disposition
    status = mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_FAILED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.FAILED
    assert rec.attempts == 1  # one genuine failure (FR-6)
    assert rec.human_responses[-1].state == M.RESPONSE_CONSUMED


# --- invocation-local schema binding (FR-10 / FR-4.1) -----------------------
def test_resume_disposition_schema_bound_invocation_locally(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    # The FIRST conflict run carries NO schema — the implement step has none, and
    # there is no response to consume yet, so the disposition schema is not bound.
    conflict = ConflictAdapter()
    status = mgr.start(
        "demo", repo / "pipelines" / "respond.yaml",
        use_judge=False, adapter_factory=lambda n: conflict, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    assert conflict.schemas == [None]

    run_dir = _run_dir(mgr)
    src_pipeline = (repo / "pipelines" / "respond.yaml").read_bytes()
    run_pipeline = (run_dir / "pipeline.yaml").read_bytes()

    adapter = DispositionAdapter(_disposition("proceed_in_place"), write=True)
    mgr.resume(
        "demo", response="Ratify option 1.", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    # The resume invocation received the resume-disposition schema.
    bound = adapter.schemas[-1]
    assert bound is not None
    assert bound["$id"] == "gauntlet/schemas/resume-disposition.json"

    # The approved pipeline definitions are byte-for-byte unchanged (FR-4.1): the
    # schema is bound invocation-locally, not written into the step config.
    assert (repo / "pipelines" / "respond.yaml").read_bytes() == src_pipeline
    assert (run_dir / "pipeline.yaml").read_bytes() == run_pipeline
    from gauntlet.engine.pipeline import load_pipeline
    pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
    implement = next(
        s for stage in pipeline.stages for s in stage.steps if s.id == "implement"
    )
    assert implement.get("schema") is None
    assert implement.get("findings_schema") is None


def test_responses_considered_lists_consumed_id(tmp_path):
    # FR-5/FR-10 observable property: the disposition references the consumed
    # response_id; a second resume references both.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    first = DispositionAdapter(_disposition("new_conflict"), write=False)
    mgr.resume(
        "demo", response="ambiguous", use_judge=False,
        adapter_factory=lambda n: first, clock=_clock(),
    )
    rec = mgr.status("demo").record("implement")
    assert rec.human_responses[-1].response_id == "implement-resp-1"

    second = DispositionAdapter(
        _disposition(
            "proceed_in_place",
            responses=("implement-resp-1", "implement-resp-2"),
        ),
        write=True,
    )
    mgr.resume(
        "demo", response="now resolved", use_judge=False,
        adapter_factory=lambda n: second, clock=_clock(),
    )
    assert second.structured["responses_considered"] == [
        "implement-resp-1", "implement-resp-2"
    ]
    rec = mgr.status("demo").record("implement")
    assert [r.response_id for r in rec.human_responses] == [
        "implement-resp-1", "implement-resp-2"
    ]
