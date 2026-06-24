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
    """Build a schema-valid disposition object for the scripted adapter.

    `conflict` is always present (required-but-nullable, F-002): an object for the
    re-park dispositions, null for the proceed dispositions.
    """
    obj: dict = {
        "disposition": disposition,
        "responses_considered": list(responses),
        "action_summary": f"{disposition} action",
        "conflict": None,
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

    def __init__(
        self,
        structured: dict | None,
        *,
        write: bool = False,
        text: str = "resume disposition emitted",
    ) -> None:
        self.structured = structured
        self.write = write
        self.text = text
        self.prompts: list[str] = []
        self.schemas: list[dict | None] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self.write:
            (Path(cwd) / "feature.py").write_text("implemented\n")
        return AgentResult(
            text=self.text,
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
def test_proceed_dispositions_validate_with_null_conflict():
    # conflict is required-but-nullable (F-002): a proceed disposition carries it
    # as null and validates; the allOf forbids an object there.
    schema = _schema()
    for d in ("proceed_in_place", "proceed_with_deviation"):
        obj = _disposition(d)
        assert obj["conflict"] is None
        validate_schema(obj, schema)  # no raise


def test_schema_strict_mode_lists_every_property_required():
    # F-002: codex 0.139.0 native structured output (strict mode) requires EVERY
    # property to appear in `required`; conflict is spelled required-but-nullable
    # (mirrors findings.json). Assert the top-level object and the nested conflict
    # object both list all their properties as required.
    schema = _schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert "conflict" in schema["required"]
    assert schema["properties"]["conflict"]["type"] == ["object", "null"]
    conflict = schema["properties"]["conflict"]
    assert set(conflict["required"]) == set(conflict["properties"])


def test_schema_rejects_empty_semantic_evidence():
    # F-003: the oracle must reject empty response evidence and empty clarification
    # text — minItems/minLength enforce non-empty semantic fields.
    schema = _schema()
    # empty responses_considered (no response evidence)
    with pytest.raises(ValueError):
        validate_schema(
            {
                "disposition": "proceed_in_place",
                "responses_considered": [],
                "action_summary": "x",
                "conflict": None,
            },
            schema,
        )
    # empty clarification request inside a re-park conflict
    with pytest.raises(ValueError):
        validate_schema(
            {
                "disposition": "new_conflict",
                "responses_considered": ["implement-resp-1"],
                "action_summary": "x",
                "conflict": {"summary": "s", "requested_input": "", "artifact": None},
            },
            schema,
        )


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


# --- responses_considered must reference the consumed response (review F-001) -
def test_disposition_omitting_consumed_response_fails_closed(tmp_path):
    # A disposition whose responses_considered does not name the pending response
    # is response-unaware: it must NOT pass the conflict gate (fail closed), even
    # though the disposition enum itself is well-formed.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = DispositionAdapter(
        _disposition("proceed_in_place", responses=()), write=False
    )
    status = mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_FAILED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.FAILED
    assert rec.attempts == 1  # a malformed resume is a genuine failure (FR-6)
    assert rec.human_responses[-1].state == M.RESPONSE_CONSUMED


def test_disposition_unknown_response_id_fails_closed(tmp_path):
    # An id that is not in the recorded history is fabricated evidence → fail closed.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = DispositionAdapter(
        _disposition("proceed_in_place", responses=("implement-resp-99",)),
        write=False,
    )
    status = mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_FAILED
    assert mgr.status("demo").record("implement").status == M.FAILED


def test_disposition_duplicate_response_id_fails_closed(tmp_path):
    # A duplicated id is malformed evidence → fail closed (no advancing on it).
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = DispositionAdapter(
        _disposition(
            "proceed_in_place",
            responses=("implement-resp-1", "implement-resp-1"),
        ),
        write=False,
    )
    status = mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_FAILED
    assert mgr.status("demo").record("implement").status == M.FAILED


def test_amendment_required_without_artifact_fails_closed(tmp_path):
    # FR-3(b) / review F-003: an amendment_required must name the approved artifact
    # it diverges from. A null target is malformed — fail closed rather than a
    # silent re-park with no named amendment site.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = DispositionAdapter(
        _disposition("amendment_required", artifact=None), write=False
    )
    status = mgr.resume(
        "demo", response="rewrite something", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_FAILED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.FAILED
    assert rec.attempts == 1


# --- proceed preserves the normal completion contract (review F-004) ---------
PIPELINE_OUTPUT = """
name: respond
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go,
         halt_on: "UPSTREAM CONFLICT", output: built.md}
"""

PIPELINE_REQUIRE_SIGNAL = """
name: respond
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go,
         halt_on: "UPSTREAM CONFLICT", require_signal: "DONE SIGNAL"}
"""


def test_proceed_resume_writes_declared_output_artifact(tmp_path):
    # F-004: a response-resumed agent_task with `output:` must still write its
    # declared artifact on a proceed disposition — the proceed branch no longer
    # short-circuits before the artifact write.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_OUTPUT)
    _drive_to_conflict(repo, mgr, PIPELINE_OUTPUT)
    artifact = mgr.layout("demo").slug_dir / "built.md"
    assert not artifact.exists()  # the conflict park wrote nothing
    adapter = DispositionAdapter(
        _disposition("proceed_in_place"), write=True, text="the built artifact body"
    )
    status = mgr.resume(
        "demo", response="Ratify option 1.", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    assert mgr.status("demo").record("implement").status == M.DONE
    assert artifact.read_text() == "the built artifact body"


def test_proceed_resume_honors_require_signal_when_absent(tmp_path):
    # F-004: `require_signal` must still bind on a proceed resume. A proceed
    # disposition whose output text lacks the required completion signal fails
    # closed — the resume branch no longer ignores require_signal.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_REQUIRE_SIGNAL)
    _drive_to_conflict(repo, mgr, PIPELINE_REQUIRE_SIGNAL)
    adapter = DispositionAdapter(
        _disposition("proceed_in_place"), write=False, text="no signal here"
    )
    status = mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_FAILED
    assert mgr.status("demo").record("implement").status == M.FAILED


def test_proceed_resume_completes_when_require_signal_present(tmp_path):
    # The complement: when the required signal IS emitted, the proceed resume
    # completes normally (the halt_on marker is suppressed, require_signal passes).
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_REQUIRE_SIGNAL)
    _drive_to_conflict(repo, mgr, PIPELINE_REQUIRE_SIGNAL)
    adapter = DispositionAdapter(
        _disposition("proceed_in_place"), write=True,
        text="DONE SIGNAL\nfinished the work",
    )
    status = mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    assert mgr.status("demo").record("implement").status == M.DONE


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


# --- F-001: a conflict re-park must hand off a CLEAN worktree ----------------
def test_repark_with_dirty_worktree_restores_clean_tree(tmp_path):
    # Review F-001 / clean-handoff invariant (CLAUDE.md §1): the builder runs with
    # repo-write access. If it writes implementation edits and THEN re-parks on a
    # conflict (write=True + a re-park disposition), those uncommitted edits must
    # not survive to the human handoff — a later `--response` resume re-enters the
    # PARKED step with resuming=False, bypassing the dirty-base recovery, and would
    # otherwise re-run over and silently commit them. The engine restores the clean
    # tree before finalizing the park, having snapshotted the work to a backup ref.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = DispositionAdapter(_disposition("new_conflict"), write=True)
    status = mgr.resume(
        "demo", response="ambiguous", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_UPSTREAM_CONFLICT
    # The builder's uncommitted edit was discarded; no implementation change
    # survives to the handoff (the only residual dirt is engine bookkeeping,
    # which the clean-handoff invariant deliberately excludes).
    assert not (repo / "feature.py").exists()
    assert "feature.py" not in gitops.status_porcelain(repo)
    # ...and preserved losslessly in a backup ref carrying that very edit.
    refs = gitops._run(
        repo, "for-each-ref", "--format=%(refname)", "refs/gauntlet/backup"
    ).splitlines()
    backup = [r for r in refs if "conflict" in r]
    assert backup, "a dirty conflict park must snapshot the work to a backup ref"
    tree = gitops._run(repo, "ls-tree", "-r", "--name-only", backup[0])
    assert "feature.py" in tree


def test_clean_repark_creates_no_backup(tmp_path):
    # The negative control: when the builder honors the classify-don't-implement
    # contract (write=False), a re-park leaves the tree already clean, so the guard
    # is a no-op — no needless reset, no backup ref.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = DispositionAdapter(_disposition("new_conflict"), write=False)
    status = mgr.resume(
        "demo", response="ambiguous", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    # No implementation edit to discard → the guard is a no-op: no backup ref.
    refs = gitops._run(
        repo, "for-each-ref", "--format=%(refname)", "refs/gauntlet/backup"
    ).strip()
    assert refs == ""


def test_dirty_repark_then_proceed_commits_no_stale_edits(tmp_path):
    # End-to-end: a dirty re-park followed by a `--response` proceed must not carry
    # the first attempt's discarded edits into the phase commit. With the clean
    # restore, the proceed run re-implements from a clean base and the committed
    # tree reflects only the proceed's own output.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE)
    _drive_to_conflict(repo, mgr, PIPELINE)
    # First resume: the builder writes, then re-parks — edits must be discarded.
    reparked = DispositionAdapter(_disposition("new_conflict"), write=True)
    assert mgr.resume(
        "demo", response="ambiguous", use_judge=False,
        adapter_factory=lambda n: reparked, clock=_clock(),
    ) == M.RUN_PARKED
    assert not (repo / "feature.py").exists()
    # Second resume: a clean proceed lands the phase commit normally.
    proceed = DispositionAdapter(
        _disposition(
            "proceed_in_place",
            responses=("implement-resp-1", "implement-resp-2"),
        ),
        write=True,
    )
    assert mgr.resume(
        "demo", response="now resolved", use_judge=False,
        adapter_factory=lambda n: proceed, clock=_clock(),
    ) == M.RUN_DONE
    assert gitops.commit_subject(repo, "HEAD") == "P1: implement phase"
    assert (repo / "feature.py").read_text() == "implemented\n"


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
