"""Ensemble review inside adversarial_cycle (FR-1.1/1.2/1.3, P1).

Drives the real handler through the orchestrator on scripted fakes (reusing the
test_cycle harness): a two-member panel persists one findings artifact per member
(P1-A1), the deterministic dedup merges the shared defect and triages each
primary once while keeping a divergent claim distinct (P1-A2), per-(profile,lens)
yield lands in the manifest metrics (P1-A3), a one-member config is byte-identical
to the single-reviewer output (P1-A4), a member error/usage-limit parks the step
fail-closed (P1-A6), and a resume re-invokes only the not-yet-completed member
(P1-A7).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from gauntlet.adapters.base import (
    FAILURE_TRANSIENT_DEPENDENCY,
    FAILURE_TRANSIENT_OVERLOAD,
    AgentFailedError,
    AgentResult,
    FailureInfo,
)
from gauntlet.engine import gitops, manifest as M

from conftest import git
from test_cycle import (
    CONFIRM,
    CV,
    F,
    REVIEW,
    SeqAdapter,
    V,
    _build_cycle_orch,
    _transient_exc,
    run_cycle,
    writer,
)

REPO = Path(__file__).resolve().parents[2]

ENS_CONFIG = {
    "triage_concurrency": 1,  # positional triage fakes stay deterministic
    "agents": {
        "reviewer": {"adapter": "codex"},
        "gemini": {"adapter": "api", "model": "g"},
        "triage": {"adapter": "api", "model": "h"},
        "builder": {"adapter": "claude-code"},
        "esc": {"adapter": "api", "model": "strong"},
    },
    "identities": {
        "reviewer": {"name": "Gauntlet Reviewer (codex)", "email": "reviewer@gauntlet.local"},
        "builder": {"name": "Gauntlet Builder (claude)", "email": "builder@gauntlet.local"},
    },
}

PANEL = {"reviewers": [
    {"profile": "reviewer", "lens": "correctness"},
    {"profile": "gemini", "lens": "spec-coverage"},
]}


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@gauntlet.local")
    git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("fixture\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    git(path, "branch", "-M", "main")
    return path


def _ens_repo(repo):
    """A git repo carrying the real schemas AND prompts (lens fragments) + seed.

    Accepts either the ``fixture_repo`` (already initialized) or a bare path,
    which is initialized first."""
    if not (repo / ".git").exists():
        _init_repo(repo)
    shutil.copytree(REPO / "schemas", repo / "schemas")
    shutil.copytree(REPO / "prompts", repo / "prompts")
    (repo / "prd.md").write_text("ARTIFACT-BODY-SENTINEL\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return repo


def _fx(fid, sev, cat, loc, claim):
    return {"id": fid, "severity": sev, "category": cat, "location": loc,
            "claim": claim, "evidence": "seen", "suggested_fix": None}


def _members_dir(run_dir):
    return run_dir / "artifacts" / "r1" / "members"


def _dependency_exc(kind, marker):
    return AgentFailedError(
        f"transient infrastructure failure [{marker}]",
        partial=AgentResult(text="", exit_code=1),
        failure_info=FailureInfo(kind=kind, marker=marker),
    )


# ===========================================================================
# P1-A1/A2/A3 — panel persists per-member artifacts, dedup merges, metrics
# ===========================================================================
def test_two_member_panel_persists_merges_and_yields_metrics(fixture_repo):
    repo = _ens_repo(fixture_repo)
    # reviewer and gemini both raise the SAME defect (src.py:1, overlapping,
    # compatible claim) → merge to one primary; gemini also raises a DISTINCT
    # finding elsewhere → kept as its own primary.
    reviewer = SeqAdapter(
        REVIEW(_fx("F-001", "major", "correctness", "src.py:1",
                   "the counter overflows the window budget")),
        CONFIRM(CV("0-reviewer-correctness:F-001", "resolved"),
                CV("1-gemini-spec-coverage:F-002", "resolved")),
    )
    gemini = SeqAdapter(REVIEW(
        _fx("F-001", "major", "correctness", "src.py:1",
            "counter overflows the window budget silently"),
        _fx("F-002", "major", "correctness", "other.py:5",
            "a distinct unrelated defect in another file"),
    ))
    adapters = {
        "reviewer": reviewer, "gemini": gemini,
        "triage": SeqAdapter(V("x"), V("y")),  # ids overwritten to the primaries'
        "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
        "esc": SeqAdapter(),
    }
    orch, man = _build_cycle_orch(repo, adapters, step_extra=PANEL, config=ENS_CONFIG)
    assert orch.drive() == M.RUN_DONE
    run_dir = repo / "runs" / "demo" / "run-1"

    # P1-A1: one persisted findings artifact per member.
    member_files = sorted(p.name for p in _members_dir(run_dir).glob("*.json"))
    assert len(member_files) == 2

    # P1-A2: the merged findings.json marks the duplicate + aggregates sources;
    # only the two primaries were triaged (dup never reaches triage).
    merged = json.loads((run_dir / "artifacts" / "findings.json").read_text())
    ids = {f["id"]: f for f in merged["findings"]}
    # ids are namespaced with the collision-free member key (index+profile+lens).
    assert ids["0-reviewer-correctness:F-001"]["sources"] == ["reviewer", "gemini"]
    assert "duplicate_of" not in ids["0-reviewer-correctness:F-001"]
    assert ids["1-gemini-spec-coverage:F-001"]["duplicate_of"] == "0-reviewer-correctness:F-001"
    assert "duplicate_of" not in ids["1-gemini-spec-coverage:F-002"]  # distinct primary
    triage_targets = {v["finding_id"] for v in
                      json.loads((run_dir / "artifacts" / "triage.json").read_text())["verdicts"]}
    assert triage_targets == {"0-reviewer-correctness:F-001",
                              "1-gemini-spec-coverage:F-002"}  # once per primary
    assert len(adapters["triage"].calls) == 2

    # P1-A3: per-(profile, lens) yield readable straight from the manifest.
    # `unique_*` is SOLE-SOURCE (PR #59 review F-004): reviewer's F-001 was also
    # raised by gemini (sources aggregate both), so it is SHARED coverage —
    # owning the primary phrasing is not unique yield, and counting it as such
    # would mask the near-total-overlap case the §1.3 kill criterion measures.
    ens = man.record("cycle").metrics["ensemble"]["unique_legit_by_member"]
    assert ens["reviewer::correctness"] == {
        "profile": "reviewer", "lens": "correctness",
        "raised": 1, "unique_after_dedup": 0, "unique_legit": 0,
    }
    assert ens["gemini::spec-coverage"] == {
        "profile": "gemini", "lens": "spec-coverage",
        "raised": 2, "unique_after_dedup": 1, "unique_legit": 1,
    }


SAME_PROFILE_PANEL = {"reviewers": [
    {"profile": "reviewer", "lens": "correctness"},
    {"profile": "reviewer", "lens": "security"},
]}


def test_same_profile_multilens_panel_yields_two_distinct_triage_targets(fixture_repo):
    # Two lenses on the SAME profile both emit `F-001` for DISTINCT defects. The
    # profile-only namespace would collapse both persisted findings to
    # `reviewer:F-001` (one ambiguous triage target); the collision-free member
    # key keeps them two stable ids, so both reach triage separately (FR-1.2).
    repo = _ens_repo(fixture_repo)
    # Same profile => one shared adapter; member reviews run in panel order
    # (correctness then security), then the confirm pass (confirmer = panel[0]).
    reviewer = SeqAdapter(
        REVIEW(_fx("F-001", "major", "correctness", "src.py:1",
                   "the loop bound is off by one on the final iteration")),
        REVIEW(_fx("F-001", "major", "security", "auth.py:5",
                   "the session token is written to the log in plaintext")),
        CONFIRM(CV("0-reviewer-correctness:F-001", "resolved"),
                CV("1-reviewer-security:F-001", "resolved")),
    )
    adapters = {
        "reviewer": reviewer, "gemini": SeqAdapter(),
        "triage": SeqAdapter(V("x"), V("y")),  # ids overwritten to the primaries'
        "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
        "esc": SeqAdapter(),
    }
    orch, _man = _build_cycle_orch(
        repo, adapters, step_extra=SAME_PROFILE_PANEL, config=ENS_CONFIG)
    assert orch.drive() == M.RUN_DONE
    run_dir = repo / "runs" / "demo" / "run-1"

    # Two persisted member artifacts (one per member, keyed by member.key).
    member_files = sorted(p.name for p in _members_dir(run_dir).glob("*.json"))
    assert len(member_files) == 2

    # Distinct claims => no merge => two primaries with distinct, member-keyed ids.
    merged = json.loads((run_dir / "artifacts" / "findings.json").read_text())
    ids = {f["id"] for f in merged["findings"]}
    assert ids == {"0-reviewer-correctness:F-001", "1-reviewer-security:F-001"}
    assert all("duplicate_of" not in f for f in merged["findings"])
    # both share the profile but carry their own lens (metrics stay separable).
    by_id = {f["id"]: f for f in merged["findings"]}
    assert by_id["0-reviewer-correctness:F-001"]["lens"] == "correctness"
    assert by_id["1-reviewer-security:F-001"]["lens"] == "security"

    # Two unique triage targets — the collision would have produced one.
    triage_targets = {v["finding_id"] for v in
                      json.loads((run_dir / "artifacts" / "triage.json").read_text())["verdicts"]}
    assert triage_targets == {"0-reviewer-correctness:F-001", "1-reviewer-security:F-001"}
    assert len(adapters["triage"].calls) == 2


# ===========================================================================
# P4-A2 — a `behavioral` finding survives merge → triage → confirm end-to-end
# ===========================================================================
def test_behavioral_finding_survives_merge_triage_confirm(fixture_repo):
    # FR-2.4 phase-order precondition: with the schema+consumer migration landed
    # (and BEFORE any verifier is wired, P5), a `category: behavioral` finding
    # must flow through the whole cycle unrejected. Both members raise the same
    # behavioral defect → the deterministic MERGE dedups them to one primary; the
    # primary is TRIAGEd once (validated as a finding), fixed, then CONFIRMed —
    # the run completing proves every category-enforcing consumer accepts it.
    repo = _ens_repo(fixture_repo)
    reviewer = SeqAdapter(
        REVIEW(_fx("F-001", "major", "behavioral", "src.py:1",
                   "the CLI exits 0 but writes no output file")),
        CONFIRM(CV("0-reviewer-correctness:F-001", "resolved")),
    )
    gemini = SeqAdapter(REVIEW(
        _fx("F-001", "major", "behavioral", "src.py:1",
            "running the CLI writes no output file despite exit 0"),
    ))
    adapters = {
        "reviewer": reviewer, "gemini": gemini,
        "triage": SeqAdapter(V("x")),  # id overwritten to the primary
        "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
        "esc": SeqAdapter(),
    }
    orch, _man = _build_cycle_orch(repo, adapters, step_extra=PANEL, config=ENS_CONFIG)
    assert orch.drive() == M.RUN_DONE  # cycle converged — nothing rejected it
    run_dir = repo / "runs" / "demo" / "run-1"

    # MERGE: the two behavioral members deduped to one primary carrying the
    # category through; the merged record still validates against the schema.
    merged = json.loads((run_dir / "artifacts" / "findings.json").read_text())
    from gauntlet.adapters._structured import validate_schema
    validate_schema(merged, json.loads((repo / "schemas" / "findings.json").read_text()))
    primary = merged["findings"][0]
    assert primary["id"] == "0-reviewer-correctness:F-001"
    assert primary["category"] == "behavioral"
    assert primary["sources"] == ["reviewer", "gemini"]

    # TRIAGE: the behavioral primary was triaged exactly once (reached triage).
    assert len(adapters["triage"].calls) == 1
    triage_targets = {v["finding_id"] for v in
                      json.loads((run_dir / "artifacts" / "triage.json").read_text())["verdicts"]}
    assert triage_targets == {"0-reviewer-correctness:F-001"}

    # CONFIRM: the behavioral finding received a confirm verdict.
    confirm = json.loads((run_dir / "artifacts" / "confirm.json").read_text())
    assert confirm["verdicts"][0]["finding_id"] == "0-reviewer-correctness:F-001"


def test_behavioral_migration_supports_the_verifier(fixture_repo):
    # FR-2.4 (P4 guarantee, still enforced): the schema + the merge consumer's
    # per-member validator accept `behavioral` end-to-end, so a verifier's
    # behavioral finding can never reach an unmigrated consumer. In P4 this landed
    # BEFORE any verifier execution; P5 has since wired the verifier on top of it
    # (engine/verify.py), so this test now confirms the migration underpins the
    # shipped verifier rather than that the verifier is absent.
    from gauntlet.engine import cycle as C
    from gauntlet.adapters._structured import validate_schema

    schema = json.loads((REPO / "schemas" / "findings.json").read_text())
    assert "behavioral" in schema["properties"]["findings"]["items"]["properties"]["category"]["enum"]
    # the STRICT per-member reviewer output schema (the merge consumer's input
    # validator, FR-1.2) also accepts behavioral end-to-end.
    reviewer_schema = C._reviewer_output_schema(schema)
    validate_schema(
        {"findings": [{"id": "F-1", "severity": "major", "category": "behavioral",
                       "location": "src.py:1", "claim": "c", "evidence": "e",
                       "suggested_fix": None}],
         "open_questions": [], "summary": "s"},
        reviewer_schema,
    )
    # P5 has now wired verifier EXECUTION on top of the P4 migration.
    assert (REPO / "src" / "gauntlet" / "engine" / "verify.py").exists()


def test_lens_fragment_reaches_member_prompt(fixture_repo):
    repo = _ens_repo(fixture_repo)
    reviewer = SeqAdapter(REVIEW())  # converge immediately
    gemini = SeqAdapter(REVIEW())
    adapters = {"reviewer": reviewer, "gemini": gemini,
                "triage": SeqAdapter(), "builder": SeqAdapter(), "esc": SeqAdapter()}
    orch, _man = _build_cycle_orch(repo, adapters, step_extra=PANEL, config=ENS_CONFIG)
    assert orch.drive() == M.RUN_DONE
    # each member's own lens fragment is appended to the shared review scope.
    assert "review lens: correctness" in reviewer.calls[0]["prompt"]
    assert "review lens: spec-coverage" in gemini.calls[0]["prompt"]
    assert "ARTIFACT-BODY-SENTINEL" in reviewer.calls[0]["prompt"]  # same scope


# ===========================================================================
# P1-A4 — a one-member config is byte-identical to the single-reviewer output
# ===========================================================================
def test_one_member_config_is_byte_identical_to_single_reviewer(fixture_repo, tmp_path):
    def _run(repo, step_extra):
        reviewer = SeqAdapter(REVIEW(F("F-001")), CONFIRM(CV("F-001")))
        adapters = {"reviewer": reviewer, "gemini": SeqAdapter(),
                    "triage": SeqAdapter(V("F-001")),
                    "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
                    "esc": SeqAdapter()}
        orch, _man = _build_cycle_orch(repo, adapters, step_extra=step_extra, config=ENS_CONFIG)
        assert orch.drive() == M.RUN_DONE
        return (repo / "runs" / "demo" / "run-1" / "artifacts" / "findings.json").read_bytes()

    single = _run(_ens_repo(fixture_repo), {})
    one_member = _run(_ens_repo(tmp_path / "repo2"), {"reviewers": ["reviewer"]})
    assert single == one_member
    # and the single-reviewer artifact carries none of the ensemble fields
    obj = json.loads(single)
    for f in obj["findings"]:
        assert not (set(f) & {"source", "lens", "duplicate_of", "sources"})


def test_one_member_panel_with_lens_applies_the_lens(fixture_repo):
    # PR #59 review F-003: byte-compat covers only the LENS-LESS one-member
    # config. A one-member panel that DECLARES a lens must actually review with
    # it (previously `_validate_panel` proved the lens file existed, then the
    # single-reviewer branch silently reviewed without it).
    repo = _ens_repo(fixture_repo)
    reviewer = SeqAdapter(REVIEW(F("F-001")), CONFIRM(CV("0-reviewer-security:F-001")))
    adapters = {"reviewer": reviewer, "gemini": SeqAdapter(),
                "triage": SeqAdapter(V("x")),
                "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
                "esc": SeqAdapter()}
    step_extra = {"reviewers": [{"profile": "reviewer", "lens": "security"}]}
    status, _man, run_dir = run_cycle(repo, adapters, step_extra=step_extra,
                                      config=ENS_CONFIG)
    assert status == M.RUN_DONE
    # the lens fragment reached the reviewer's prompt
    assert "security** member of the review panel" in reviewer.calls[0]["prompt"]
    # and the member machinery ran: per-member artifact + stamped ensemble fields
    assert len(list(_members_dir(run_dir).glob("*.json"))) == 1
    merged = json.loads((run_dir / "artifacts" / "findings.json").read_text())
    assert merged["findings"][0]["source"] == "reviewer"
    assert merged["findings"][0]["lens"] == "security"


def test_legit_by_member_excludes_verifier_and_carried_phantoms():
    # PR #59 review F-002: verifier findings (source "verifier") and carried
    # remainders (no source, engine-synthesized legitimate verdicts) are not
    # panel yield — un-filtered they minted phantom "verifier::nolens" /
    # "None::nolens" members in metrics.ensemble.unique_legit_by_member, the
    # exact field the §9 panel-shrink governance consumes.
    from gauntlet.engine.cycle import PanelMember, _ensemble_legit_by_member

    panel = [PanelMember(profile="reviewer", lens="correctness", index=0)]
    primaries = [
        {"id": "A", "source": "reviewer", "lens": "correctness",
         "sources": ["reviewer"]},
        {"id": "B", "source": "verifier", "lens": None},          # behavioral
        {"id": "F-1-r1-c0", "carried_from": "F-1"},               # remainder
    ]
    verdicts = [{"finding_id": fid, "verdict": "legitimate"} for fid in
                ("A", "B", "F-1-r1-c0")]
    assert _ensemble_legit_by_member(primaries, verdicts, panel) == {
        "reviewer::correctness": 1
    }


def test_shared_primary_is_not_unique_legit_for_its_owner():
    # PR #59 review F-004 at the helper level: a primary with two sources is
    # shared coverage; it counts toward NEITHER member's unique yield.
    from gauntlet.engine.cycle import PanelMember, _ensemble_legit_by_member

    panel = [PanelMember(profile="reviewer", lens="correctness", index=0),
             PanelMember(profile="gemini", lens="spec-coverage", index=1)]
    primaries = [{"id": "A", "source": "reviewer", "lens": "correctness",
                  "sources": ["reviewer", "gemini"],
                  "source_members": ["reviewer::correctness", "gemini::spec-coverage"]}]
    verdicts = [{"finding_id": "A", "verdict": "legitimate"}]
    assert _ensemble_legit_by_member(primaries, verdicts, panel) == {}


def test_same_profile_two_lens_shared_primary_is_not_unique_legit():
    # PR #59 review F-005: the same profile is a valid panel entry under two
    # lenses. Both members raised A, so `sources` collapses to ["reviewer"] —
    # counting profiles would read A as sole-source and credit the owning lens
    # with unique yield it did not earn, corrupting the §1.3 kill criterion in
    # the direction that keeps a redundant panel alive.
    from gauntlet.engine.cycle import PanelMember, _ensemble_legit_by_member

    panel = [PanelMember(profile="reviewer", lens="correctness", index=0),
             PanelMember(profile="reviewer", lens="security", index=1)]
    primaries = [{"id": "A", "source": "reviewer", "lens": "correctness",
                  "sources": ["reviewer"],  # collapsed: one profile, two members
                  "source_members": ["reviewer::correctness", "reviewer::security"]}]
    verdicts = [{"finding_id": "A", "verdict": "legitimate"}]
    assert _ensemble_legit_by_member(primaries, verdicts, panel) == {}


def test_legacy_primary_without_source_members_falls_back_to_sources():
    # A pre-migration artifact has no member data to recover; the profile-level
    # count is the best available answer and must still work (schema keeps
    # source_members optional, so a legacy findings.json validates and reads).
    from gauntlet.engine.cycle import PanelMember, _ensemble_legit_by_member

    panel = [PanelMember(profile="reviewer", lens="correctness", index=0),
             PanelMember(profile="gemini", lens="spec-coverage", index=1)]
    sole = [{"id": "A", "source": "reviewer", "lens": "correctness",
             "sources": ["reviewer"]}]
    shared = [{"id": "B", "source": "reviewer", "lens": "correctness",
               "sources": ["reviewer", "gemini"]}]
    assert _ensemble_legit_by_member(
        sole, [{"finding_id": "A", "verdict": "legitimate"}], panel
    ) == {"reviewer::correctness": 1}
    assert _ensemble_legit_by_member(
        shared, [{"finding_id": "B", "verdict": "legitimate"}], panel
    ) == {}


# ===========================================================================
# P1-A6 — a member error / usage-limit parks the ensemble step FAIL CLOSED
# ===========================================================================
def test_member_terminal_error_parks_fail_closed_no_triage(fixture_repo):
    repo = _ens_repo(fixture_repo)
    from gauntlet.adapters.base import AgentFailedError

    reviewer = SeqAdapter(REVIEW(F("F-001")))  # member 1 completes
    gemini = SeqAdapter(AgentFailedError("terminal boom"))  # member 2 dies (terminal)
    triage = SeqAdapter()  # must NEVER be called
    adapters = {"reviewer": reviewer, "gemini": gemini,
                "triage": triage, "builder": SeqAdapter(), "esc": SeqAdapter()}
    status, man, run_dir = run_cycle(repo, adapters, step_extra=PANEL, config=ENS_CONFIG)

    assert status == M.RUN_PARKED  # never proceeds on a reduced panel
    assert len(triage.calls) == 0  # dedup/triage never ran
    assert "fail-closed" in man.record("cycle").notes
    # member 1's artifact persisted; member 2's absent (not treated as clean).
    files = {p.name for p in _members_dir(run_dir).glob("*.json")}
    assert any("reviewer" in n for n in files)
    assert not any("gemini" in n for n in files)


def test_member_usage_limit_parks_resumably(fixture_repo):
    repo = _ens_repo(fixture_repo)
    reviewer = SeqAdapter(REVIEW(F("F-001")))
    gemini = SeqAdapter(_transient_exc(session="gemini-sess"))
    adapters = {"reviewer": reviewer, "gemini": gemini,
                "triage": SeqAdapter(), "builder": SeqAdapter(), "esc": SeqAdapter()}
    status, man, _ = run_cycle(repo, adapters, step_extra=PANEL, config=ENS_CONFIG)
    assert status == M.RUN_PARKED
    assert man.record("cycle").parked_reason == M.PARKED_REASON_USAGE_LIMIT


def test_member_capacity_failure_retries_in_process_without_human_park(
    fixture_repo, monkeypatch
):
    # Issue #119: once the exact capacity envelope is classified transient, the
    # existing _run_sub/depretry path retries the missing member in-process. The
    # full panel completes and triage proceeds without a parked_for_response turn.
    from gauntlet.engine import depretry

    waits = []
    monkeypatch.setattr(depretry, "_sleep", waits.append)
    repo = _ens_repo(fixture_repo)
    fid = "0-reviewer-correctness:F-001"
    reviewer = SeqAdapter(
        REVIEW(_fx("F-001", "major", "correctness", "src.py:1", "defect")),
        CONFIRM(CV(fid, "resolved")),
    )
    gemini = SeqAdapter(
        _dependency_exc(FAILURE_TRANSIENT_OVERLOAD, "codex_capacity_message"),
        REVIEW(),
    )
    triage = SeqAdapter(V(fid))
    adapters = {
        "reviewer": reviewer,
        "gemini": gemini,
        "triage": triage,
        "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
        "esc": SeqAdapter(),
    }
    config = {
        **ENS_CONFIG,
        "dependency_retry_attempts": 2,
        "dependency_retry_base_s": 0.01,
        "dependency_retry_max_delay_s": 1.0,
    }
    status, man, _ = run_cycle(repo, adapters, step_extra=PANEL, config=config)
    assert status == M.RUN_DONE
    assert len(gemini.calls) == 2 and len(waits) == 1
    assert len(triage.calls) == 1
    assert man.record("cycle").dependency_attempts == 0


def test_member_cache_failure_exhaustion_parks_provider_then_plain_resumes(
    fixture_repo, monkeypatch
):
    # Exhaustion stays fail-closed (no panel shrink) but is an infrastructure
    # park with a plain resume. The completed first member is content-addressed
    # and reused; only the cache-failing member is repaid after recovery.
    from gauntlet.engine import depretry

    waits = []
    monkeypatch.setattr(depretry, "_sleep", waits.append)
    repo = _ens_repo(fixture_repo)
    fid = "0-reviewer-correctness:F-001"
    reviewer = SeqAdapter(
        REVIEW(_fx("F-001", "major", "correctness", "src.py:1", "defect")),
        CONFIRM(CV(fid, "resolved")),
    )
    failure = lambda: _dependency_exc(
        FAILURE_TRANSIENT_DEPENDENCY,
        "codex_models_cache_schema_startup",
    )
    gemini = SeqAdapter(failure(), failure(), failure(), REVIEW())
    triage = SeqAdapter(V(fid))
    adapters = {
        "reviewer": reviewer,
        "gemini": gemini,
        "triage": triage,
        "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
        "esc": SeqAdapter(),
    }
    config = {
        **ENS_CONFIG,
        "dependency_retry_attempts": 2,
        "dependency_retry_base_s": 0.01,
        "dependency_retry_max_delay_s": 1.0,
    }
    orch, man = _build_cycle_orch(
        repo, adapters, step_extra=PANEL, config=config
    )

    assert orch.drive() == M.RUN_PARKED
    rec = man.record("cycle")
    assert rec.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert rec.dependency_attempts == 2 and rec.quota_reset_at is not None
    assert "no `--response`" in rec.notes
    assert len(reviewer.calls) == 1 and len(gemini.calls) == 3
    assert len(triage.calls) == 0
    assert len(list(_members_dir(repo / "runs/demo/run-1").glob("*.json"))) == 1

    assert orch.drive() == M.RUN_DONE  # plain resume; provider/cache recovered
    assert len(reviewer.calls) == 2  # review artifact reused; only confirm is new
    assert len(gemini.calls) == 4
    assert man.record("cycle").dependency_attempts == 0


# ===========================================================================
# P1-A7 — resume re-invokes ONLY the not-yet-completed member
# ===========================================================================
def test_resume_reuses_completed_member_and_reruns_only_incomplete(fixture_repo):
    repo = _ens_repo(fixture_repo)
    # Member 1 (reviewer) completes; member 2 (gemini) hits a usage limit → park.
    # reviewer gets EXACTLY two responses: its round-1 review (drive 1) and the
    # confirm pass (resume). If the resume re-invoked member 1, the adapter would
    # exhaust — so a passing run proves the completed member was NOT re-paid.
    reviewer = SeqAdapter(
        REVIEW(_fx("F-001", "major", "correctness", "src.py:1", "shared defect here")),
        CONFIRM(CV("0-reviewer-correctness:F-001", "resolved")),
    )
    gemini = SeqAdapter(
        _transient_exc(session="gemini-sess"),                      # drive 1: park
        REVIEW(),                                                    # resume: no findings
    )
    triage = SeqAdapter(V("0-reviewer-correctness:F-001"))
    builder = SeqAdapter(writer("src.py", "fixed\n", {"done": True}))
    adapters = {"reviewer": reviewer, "gemini": gemini,
                "triage": triage, "builder": builder, "esc": SeqAdapter()}
    orch, man = _build_cycle_orch(repo, adapters, step_extra=PANEL, config=ENS_CONFIG)

    assert orch.drive() == M.RUN_PARKED
    assert man.record("cycle").parked_reason == M.PARKED_REASON_USAGE_LIMIT
    assert len(reviewer.calls) == 1  # only the review ran on drive 1

    assert orch.drive() == M.RUN_DONE
    # reviewer was NOT re-invoked for review on resume (reused): its only new call
    # is the confirm pass. gemini re-ran (the incomplete member).
    assert len(reviewer.calls) == 2
    assert len(gemini.calls) == 2
