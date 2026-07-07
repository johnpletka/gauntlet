"""Declined-registry precedent injection through a real adversarial cycle (P6).

P6-A1: a fingerprint-matching decline recorded under current provenance surfaces
in the triage prompt as advisory data. P6-A4: `metrics.registry.rematched` is
emitted to the run metrics, and an injected matching precedent can still be
triaged legitimate (no suppression) — counted as an override, the finding
survives. Drives the real cycle handler on scripted fakes via the test_cycle
harness.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from gauntlet.engine import manifest as M
from gauntlet.engine import registry as reg
from gauntlet.logging.redact import RedactingWriter

from test_cycle import CONFIRM, CV, F, REVIEW, SeqAdapter, V, run_cycle, writer

REPO = Path(__file__).resolve().parents[2]


def _repo_with_assets(fixture_repo: Path) -> Path:
    """Fixture repo carrying the real schemas + prompts (so governed-asset hashes
    resolve) plus the seed artifact, all committed (clean handoff)."""
    shutil.copytree(REPO / "schemas", fixture_repo / "schemas")
    shutil.copytree(REPO / "prompts", fixture_repo / "prompts")
    (fixture_repo / "prd.md").write_text("ARTIFACT-BODY-SENTINEL\n")
    subprocess.run(["git", "-C", str(fixture_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(fixture_repo), "commit", "-qm", "seed"], check=True)
    return fixture_repo


def _seed_registry(repo: Path, finding: dict, *, verdict: str = "bikeshedding") -> None:
    """Append an in-force declined precedent matching ``finding`` (slug 'demo')."""
    entry = reg.DeclinedEntry(
        fingerprint=reg.finding_fingerprint(finding),
        verdict=verdict,
        reasoning="PRECEDENT-REASONING-SENTINEL: declined as taste last run",
        repo=reg.repo_name(repo),
        prd_family="demo",  # run_cycle uses slug='demo'
        prompt_version=reg.triage_version(repo, "."),
        lens_version="none",
        schema_version=reg.findings_schema_version(repo, "."),
        run_id="run-prior",
        by="triage",
        at="2026-01-01T00:00:00Z",
    )
    reg.append_entries([entry], reg.registry_path(repo, "."), RedactingWriter())
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "registry"], check=True)


def test_matching_precedent_injects_and_metric_emitted(fixture_repo):
    """P6-A1 + P6-A4: precedent surfaces in the triage prompt; rematched counted;
    a triager that classifies the match legitimate is an override (no suppression)
    and the finding survives to a fix."""
    repo = _repo_with_assets(fixture_repo)
    finding = F("F-001")
    _seed_registry(repo, finding)

    triage = SeqAdapter(V("F-001", verdict="legitimate"))
    adapters = {
        "reviewer": SeqAdapter(REVIEW(finding), CONFIRM(CV("F-001"))),
        "triage": triage,
        "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
    }
    status, man, _ = run_cycle(repo, adapters)
    assert status == M.RUN_DONE

    # P6-A1: the precedent block reached the triage prompt as advisory data.
    triage_prompt = triage.calls[0]["prompt"]
    assert "ADVISORY" in triage_prompt
    assert "PRECEDENT-REASONING-SENTINEL" in triage_prompt
    assert "prior verdict: bikeshedding" in triage_prompt

    # P6-A4: the re-litigation metric is on the manifest, readable without logs.
    rec = man.record("cycle")
    assert rec.metrics["registry"]["rematched"] == 1
    # Non-suppression: the triager overrode the precedent to legitimate — counted,
    # and the finding survived (it was accepted and fixed → a fix-round commit).
    assert rec.metrics["registry"]["injected_precedent_override_count"] == 1
    assert [c.phase for c in man.commits] == ["P5.1"]


def test_non_matching_finding_no_injection(fixture_repo):
    """A registry entry for one fingerprint does not inject for a different one;
    rematched stays 0 (registry-aware run still emits the key)."""
    repo = _repo_with_assets(fixture_repo)
    _seed_registry(repo, F("F-001", claim="a stylistic naming nit"))

    other = F("F-002", claim="a concurrency race on shutdown teardown")
    triage = SeqAdapter(V("F-002"))
    adapters = {
        "reviewer": SeqAdapter(REVIEW(other), CONFIRM(CV("F-002"))),
        "triage": triage,
        "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
    }
    status, man, _ = run_cycle(repo, adapters)
    assert status == M.RUN_DONE
    assert "ADVISORY" not in triage.calls[0]["prompt"]
    rec = man.record("cycle")
    assert rec.metrics["registry"]["rematched"] == 0


def test_stale_provenance_withheld_in_cycle(fixture_repo):
    """A precedent recorded against a since-edited triage.md is not injected
    (in-force is the content-hash identity), though it remains in the file."""
    repo = _repo_with_assets(fixture_repo)
    finding = F("F-001")
    # Record with a stale prompt_version that cannot match the current file hash.
    entry = reg.DeclinedEntry(
        fingerprint=reg.finding_fingerprint(finding),
        verdict="bikeshedding", reasoning="stale precedent",
        repo=reg.repo_name(repo), prd_family="demo",
        prompt_version="triage@deadbee", lens_version="none",
        schema_version=reg.findings_schema_version(repo, "."),
        run_id="run-prior", by="triage", at="2026-01-01T00:00:00Z",
    )
    reg.append_entries([entry], reg.registry_path(repo, "."), RedactingWriter())
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "registry"], check=True)

    triage = SeqAdapter(V("F-001"))
    adapters = {
        "reviewer": SeqAdapter(REVIEW(finding), CONFIRM(CV("F-001"))),
        "triage": triage,
        "builder": SeqAdapter(writer("src.py", "fixed\n", {"done": True})),
    }
    status, man, _ = run_cycle(repo, adapters)
    assert status == M.RUN_DONE
    assert "ADVISORY" not in triage.calls[0]["prompt"]
    assert man.record("cycle").metrics["registry"]["rematched"] == 0
