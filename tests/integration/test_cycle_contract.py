"""P4 contract test: one real adversarial_cycle round, codex as reviewer.

Drives the cycle machinery end-to-end on a disposable fixture repo with a
planted defect: REAL `codex exec -s read-only --output-schema` produces the
findings (the FR-5.2 review path this phase is built on), while triage/
escalation/fixer are deterministic fakes so the test asserts machinery, not
model opinions. Verifies: schema-valid findings on the live codex path, the
clean-handoff invariant, a format-valid `PN.x` fix-round commit when findings
are accepted, and a diff-scoped confirm round-trip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gauntlet.adapters.base import AgentResult
from gauntlet.adapters.codex import CodexAdapter
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import CommitRecord, Manifest, PipelineRef
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]

BUGGY = '''\
"""Tiny maths helpers."""


def mean(values):
    # BUG: crashes on an empty list and silently truncates to int division
    return sum(values) // len(values)
'''

FIXED = '''\
"""Tiny maths helpers."""


def mean(values):
    if not values:
        raise ValueError("mean() of an empty sequence")
    return sum(values) / len(values)
'''


@pytest.fixture(autouse=True)
def _need_codex():
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not installed")


class ConstantAdapter:
    """Same structured response for every call (triager/escalation fakes)."""

    def __init__(self, structured):
        self.structured = structured
        self.calls = 0
        self.timeout_s = 600.0

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.calls += 1
        return AgentResult(
            text=json.dumps(self.structured), structured=self.structured, exit_code=0
        )


class FixerAdapter:
    """Deterministically repairs the planted defect."""

    def __init__(self):
        self.timeout_s = 600.0

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        (Path(cwd) / "maths.py").write_text(FIXED)
        return AgentResult(text="fixed", exit_code=0)


def test_real_codex_cycle_round(fixture_repo):
    shutil.copytree(REPO / "schemas", fixture_repo / "schemas")
    (fixture_repo / "maths.py").write_text(BUGGY)
    subprocess.run(["git", "-C", str(fixture_repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(fixture_repo), "-c", "user.name=Gauntlet Test",
         "-c", "user.email=test@gauntlet.local", "commit", "-qm",
         "P1: Add maths helpers\n\nPlanted-defect fixture for the P4 cycle contract test."],
        check=True,
    )
    phase_sha = gitops.head_sha(fixture_repo)

    verdict = {"finding_id": "X", "verdict": "legitimate", "action": "fix_now",
               "confidence": "high", "target_artifact": None,
               "reasoning": "The defect is real and worth fixing this round."}
    adapters = {
        "reviewer": CodexAdapter(sandbox="read-only"),
        "triage": ConstantAdapter(verdict),
        "esc": ConstantAdapter(verdict),
        "builder": FixerAdapter(),
    }
    config = RunConfig.model_validate({
        "agents": {
            "reviewer": {"adapter": "codex", "sandbox": "read-only"},
            "triage": {"adapter": "api", "model": "gpt-5-mini"},
            "esc": {"adapter": "api", "model": "gpt-5"},
            "builder": {"adapter": "claude-code"},
        },
        "identities": {
            "reviewer": {"name": "Gauntlet Reviewer (codex)",
                         "email": "reviewer@gauntlet.local"},
            "builder": {"name": "Gauntlet Builder (claude)",
                        "email": "builder@gauntlet.local"},
        },
    })
    pipeline = Pipeline.model_validate({
        "name": "contract", "version": 1,
        "stages": [{"id": "s", "steps": [{
            "id": "cycle", "type": "adversarial_cycle", "mode": "code_review",
            "reviewer": "reviewer", "triager": "triage", "fixer": "builder",
            "escalation_agent": "esc", "max_rounds": 1,
        }]}],
    })
    man = Manifest(run_id="r", slug="contract", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="contract", version=1, hash="h"))
    man.commits.append(CommitRecord(step_id="commit", phase="P1", sha=phase_sha))
    run_dir = fixture_repo / "runs" / "contract" / "run-1"
    orch = Orchestrator(
        repo_root=fixture_repo, run_dir=run_dir, artifact_root=fixture_repo,
        config=config, pipeline=pipeline, manifest=man,
        adapter_factory=lambda n: adapters[n],
    )
    status = orch.drive()

    # Machinery assertions (model-opinion-independent):
    # 1. real codex returned schema-valid findings via --output-schema
    findings = json.loads((run_dir / "artifacts" / "findings.json").read_text())
    from gauntlet.adapters._structured import validate_schema

    validate_schema(findings, json.loads((REPO / "schemas" / "findings.json").read_text()))
    # 2. the run reached a defined terminal state, never a crash
    assert status in (M.RUN_DONE, M.RUN_PARKED)
    # 3. full FR-4 sub-step transcripts for the live round
    assert (run_dir / "steps" / "cycle" / "r1-review" / "events.jsonl").exists()
    # 4. if codex found anything (it should — the defect is planted), the fix
    #    round committed in the enforced format with fixer identity, the tree
    #    is clean, and the confirm pass round-tripped on the range diff
    if findings["findings"]:
        cycle_commits = [c for c in man.commits if c.step_id == "cycle"]
        assert cycle_commits and cycle_commits[0].phase == "P1.1"
        msg = gitops.commit_message(fixture_repo, cycle_commits[0].sha)
        assert msg.startswith("P1.1: Address review — ")
        assert gitops.is_clean(fixture_repo, exclude=["runs"])
        confirm = json.loads((run_dir / "artifacts" / "confirm.json").read_text())
        assert confirm["verdicts"], "confirm pass returned no per-finding verdicts"
