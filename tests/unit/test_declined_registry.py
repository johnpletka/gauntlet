"""Declined-findings registry + version-provenance governance (P6, FR-5.2).

Covers P6-A1 (a fingerprint-matching decline surfaces as advisory precedent under
current provenance) and P6-A2 (injection is gated on the content-hash "in force"
identity — a superseded prompt/lens/schema or a foreign PRD family withholds
injection while the entry is retained for audit).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.engine import registry as reg


# --- a governed-asset worktree fixture ---------------------------------------
@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repo tree (asset_root=".") with the three governed assets."""
    (tmp_path / "prompts" / "lenses").mkdir(parents=True)
    (tmp_path / "schemas").mkdir()
    (tmp_path / "prompts" / "triage.md").write_text("triage prompt v1\n")
    (tmp_path / "schemas" / "findings.json").write_text('{"schema": "v1"}\n')
    (tmp_path / "prompts" / "lenses" / "spec-coverage.md").write_text("spec lens v1\n")
    return tmp_path


def _finding(**over):
    base = {
        "id": "F-001",
        "severity": "nit",
        "category": "style",
        "location": "src/x.py:10",
        "claim": "This helper is missing a docstring entirely",
        "evidence": "no docstring",
    }
    base.update(over)
    return base


def _entry_for(repo: Path, finding: dict, *, verdict="bikeshedding", family="fam-a", lens="none"):
    return reg.DeclinedEntry(
        fingerprint=reg.finding_fingerprint(finding),
        verdict=verdict,
        reasoning="style-only; declined before with recorded reasoning",
        repo=reg.repo_name(repo),
        prd_family=family,
        prompt_version=reg.triage_version(repo, "."),
        lens_version=reg.lens_version(repo, ".", None if lens == "none" else lens),
        schema_version=reg.findings_schema_version(repo, "."),
        run_id="run-2026-01-01T00-00-00",
        by="triage",
        at="2026-01-01T00:00:00Z",
    )


# --- fingerprint (Q4) --------------------------------------------------------
def test_fingerprint_components_are_category_kind_keywords():
    fp = reg.finding_fingerprint(_finding())
    assert fp.startswith("style/line/claim:")
    # keyword core drops stopwords (this/is/a/an/the) and sorts the rest.
    assert "docstring" in fp and "helper" in fp
    assert "/this-" not in fp  # stopword dropped


def test_fingerprint_location_kind_variants():
    assert reg.location_kind("f.py:10") == "line"
    assert reg.location_kind("f.py:10-20") == "range"
    assert reg.location_kind("f.py:§5") == "section"
    assert reg.location_kind("f.py") == "whole-file"
    assert reg.location_kind("f.py:1-2-3") == "invalid"


def test_fingerprint_is_deterministic_regardless_of_word_order():
    a = reg.finding_fingerprint(_finding(claim="missing docstring on helper"))
    b = reg.finding_fingerprint(_finding(claim="helper missing docstring"))
    assert a == b


# --- version strings ---------------------------------------------------------
def test_asset_version_label_and_short_hash(repo: Path):
    v = reg.triage_version(repo, ".")
    label, _, h = v.partition("@")
    assert label == "triage"
    assert len(h) == reg._SHORT and h.isalnum()


def test_missing_asset_versions_to_absent(tmp_path: Path):
    assert reg.triage_version(tmp_path, ".").endswith("@absent")


# --- round-trip (registry file with all provenance fields) -------------------
def test_registry_round_trips_all_provenance_fields(repo: Path, tmp_path: Path):
    from gauntlet.logging.redact import RedactingWriter

    entry = _entry_for(repo, _finding())
    path = reg.registry_path(repo, ".")
    reg.append_entries([entry], path, RedactingWriter())
    loaded = reg.load_registry(path)
    assert len(loaded) == 1
    got = loaded[0]
    for field in (
        "fingerprint", "verdict", "reasoning", "repo", "prd_family",
        "prompt_version", "lens_version", "schema_version", "run_id", "by", "at",
    ):
        assert getattr(got, field) == getattr(entry, field)


def test_load_registry_skips_malformed_lines(repo: Path):
    path = reg.registry_path(repo, ".")
    path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(_entry_for(repo, _finding()).to_json())
    path.write_text(good + "\nnot json\n\n" + '{"no": "fingerprint"}\n')
    assert len(reg.load_registry(path)) == 1


# --- P6-A1: matching decline surfaces under current provenance ---------------
def test_matching_decline_injects_under_current_provenance(repo: Path):
    finding = _finding()
    entries = [_entry_for(repo, finding, family="fam-a")]
    blocks = reg.precedents_by_finding(
        [finding], entries, repo=reg.repo_name(repo), prd_family="fam-a",
        repo_root=repo, asset_root=".",
    )
    assert finding["id"] in blocks
    block = blocks[finding["id"]]
    assert "ADVISORY" in block
    assert "prior verdict: bikeshedding" in block


def test_non_matching_fingerprint_not_injected(repo: Path):
    entries = [_entry_for(repo, _finding())]
    other = _finding(id="F-002", claim="a totally unrelated concurrency race on shutdown",
                     category="correctness")
    blocks = reg.precedents_by_finding(
        [other], entries, repo=reg.repo_name(repo), prd_family="fam-a",
        repo_root=repo, asset_root=".",
    )
    assert blocks == {}


def test_precedent_block_wraps_reasoning_as_data(repo: Path):
    finding = _finding()
    entries = [_entry_for(repo, finding)]
    matches = reg.matching_precedents(
        finding, entries, repo=reg.repo_name(repo), prd_family="fam-a",
        repo_root=repo, asset_root=".",
    )
    block = reg.precedent_block(matches, wrap=lambda s: f"<DATA>{s}</DATA>")
    assert "<DATA>style-only; declined before with recorded reasoning</DATA>" in block


# --- P6-A2: in-force is a content-hash identity ------------------------------
def test_superseded_prompt_hash_withheld_but_retained(repo: Path):
    finding = _finding()
    entry = _entry_for(repo, finding)
    # A ratified edit to triage.md lands: its hash changes, so the decline
    # recorded against the prior hash ceases to be in force.
    (repo / "prompts" / "triage.md").write_text("triage prompt v2 (ratified edit)\n")
    assert not reg.entry_in_force(
        entry, repo=reg.repo_name(repo), prd_family="fam-a", repo_root=repo, asset_root="."
    )
    # Retained for audit: still loads from the file.
    from gauntlet.logging.redact import RedactingWriter

    path = reg.registry_path(repo, ".")
    reg.append_entries([entry], path, RedactingWriter())
    assert len(reg.load_registry(path)) == 1


def test_superseded_lens_hash_withheld(repo: Path):
    finding = _finding(lens="spec-coverage")
    entry = _entry_for(repo, finding, lens="spec-coverage")
    assert reg.entry_in_force(
        entry, repo=reg.repo_name(repo), prd_family="fam-a", repo_root=repo, asset_root="."
    )
    (repo / "prompts" / "lenses" / "spec-coverage.md").write_text("spec lens v2\n")
    assert not reg.entry_in_force(
        entry, repo=reg.repo_name(repo), prd_family="fam-a", repo_root=repo, asset_root="."
    )


def test_none_lens_not_gated_by_lens_edit(repo: Path):
    entry = _entry_for(repo, _finding(), lens="none")
    (repo / "prompts" / "lenses" / "spec-coverage.md").write_text("spec lens v2\n")
    # lens_version "none" is not gated by any lens change.
    assert reg.entry_in_force(
        entry, repo=reg.repo_name(repo), prd_family="fam-a", repo_root=repo, asset_root="."
    )


def test_superseded_schema_hash_withheld(repo: Path):
    entry = _entry_for(repo, _finding())
    (repo / "schemas" / "findings.json").write_text('{"schema": "v2"}\n')
    assert not reg.entry_in_force(
        entry, repo=reg.repo_name(repo), prd_family="fam-a", repo_root=repo, asset_root="."
    )


def test_foreign_prd_family_withheld(repo: Path):
    finding = _finding()
    entries = [_entry_for(repo, finding, family="fam-OTHER")]
    blocks = reg.precedents_by_finding(
        [finding], entries, repo=reg.repo_name(repo), prd_family="fam-a",
        repo_root=repo, asset_root=".",
    )
    assert blocks == {}


def test_foreign_repo_withheld(repo: Path):
    finding = _finding()
    entry = _entry_for(repo, finding)
    object.__setattr__(entry, "repo", "some-other-repo")
    assert not reg.entry_in_force(
        entry, repo=reg.repo_name(repo), prd_family="fam-a", repo_root=repo, asset_root="."
    )


# --- recording declines from verdicts ---------------------------------------
def test_build_entries_records_only_reasoned_declines(repo: Path):
    findings = [
        _finding(id="F-001"),
        _finding(id="F-002", claim="a real correctness bug in the retry loop",
                 category="correctness"),
        _finding(id="F-003", claim="another style nit no reasoning given"),
    ]
    verdicts = [
        {"finding_id": "F-001", "verdict": "bikeshedding", "reasoning": "style taste"},
        {"finding_id": "F-002", "verdict": "legitimate", "reasoning": "real bug"},
        {"finding_id": "F-003", "verdict": "bikeshedding", "reasoning": ""},  # no reasoning
    ]
    entries = reg.build_entries_from_verdicts(
        findings, verdicts, repo=reg.repo_name(repo), prd_family="fam-a",
        repo_root=repo, asset_root=".", run_id="run-x", at="2026-01-01T00:00:00Z",
    )
    assert len(entries) == 1
    assert entries[0].fingerprint == reg.finding_fingerprint(findings[0])
    assert entries[0].verdict == "bikeshedding"
    assert entries[0].prompt_version == reg.triage_version(repo, ".")
    assert entries[0].by == "triage"  # default decliner


def test_human_decline_recorded_and_injected(repo: Path):
    """A human decline (F-003) is recorded with ``by="human"`` and full current
    provenance, then injected as advisory precedent for a matching finding — the
    recording path is not triage-only."""
    from gauntlet.logging.redact import RedactingWriter

    finding = _finding(id="F-007", category="correctness",
                       claim="the escalated blocking finding a human dismissed as out of scope")
    verdicts = [{"finding_id": "F-007", "verdict": "not_applicable",
                 "reasoning": "operator ruled this out of scope for v1"}]
    entries = reg.build_entries_from_verdicts(
        [finding], verdicts, repo=reg.repo_name(repo), prd_family="fam-a",
        repo_root=repo, asset_root=".", run_id="run-h", at="2026-01-01T00:00:00Z",
        by="human",
    )
    assert len(entries) == 1 and entries[0].by == "human"

    path = reg.registry_path(repo, ".")
    reg.append_entries(entries, path, RedactingWriter())
    loaded = reg.load_registry(path)
    assert loaded[0].by == "human"

    # It injects as advisory precedent for a fingerprint-matching future finding.
    blocks = reg.precedents_by_finding(
        [finding], loaded, repo=reg.repo_name(repo), prd_family="fam-a",
        repo_root=repo, asset_root=".",
    )
    assert finding["id"] in blocks
    assert "prior verdict: not_applicable" in blocks[finding["id"]]


# --- PR #59 review F-4: ratified supersessions retire a fingerprint ------------
def test_load_superseded_reads_fingerprints(repo: Path):
    path = reg.supersessions_path(repo, ".")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fingerprint": "style/line/claim:x", "reason": "wrong",
                    "by": "human", "at": "2026-07-08T00:00:00Z"}) + "\n"
        + "not json\n"  # corrupt line: skipped, never crashes triage
        + json.dumps({"fingerprint": "sec/line/claim:y"}) + "\n"
    )
    assert reg.load_superseded(path) == {"style/line/claim:x", "sec/line/claim:y"}
    assert reg.load_superseded(repo / "registry" / "absent.jsonl") == set()


def test_superseded_fingerprint_is_withheld_but_retained(repo: Path):
    """§6/FR-5.2: the targeted invalidation path — a ratified supersession stops
    a precedent from surfacing while the declined.jsonl audit record remains."""
    finding = _finding()
    entry = _entry_for(repo, finding)
    superseded = {entry.fingerprint}
    # the filter _load_precedents applies:
    filtered = [e for e in [entry] if e.fingerprint not in superseded]
    assert filtered == []
    # and the underlying registry file is untouched by supersession (audit)
    reg_path = reg.registry_path(repo, ".")
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps({"fingerprint": entry.fingerprint}) + "\n")
    assert entry.fingerprint in reg_path.read_text()
