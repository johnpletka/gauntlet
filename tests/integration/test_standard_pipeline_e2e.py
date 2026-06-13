"""P5 end-to-end: the full `standard` pipeline on the toy PRD, live CLIs.

This is the FR-10.1 / FR-3 acceptance run: a human-authored toy PRD
(`tests/fixtures/toy/prd.md`) taken prd → plan → phase(s) → commits end-to-end
through `gauntlet run`, with the harness driving everything between the human
gates (which the test approves programmatically). It needs the real claude +
codex CLIs authenticated and an API key for the cheap tier, so it is marked
`integration` and skipped by default (`uv run pytest` runs units only).

Convergence depends on live models, so the assertions are deliberately
structural — the PRD/plan cycles ran, the phase loop produced `slugify.py` with
passing tests, the branch history matches FR-9, and the cost report attributes
spend per profile with classification well under the run total (FR-3).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.report import build_report
from gauntlet.engine.run import RunManager

pytestmark = [pytest.mark.integration]

REPO = Path(__file__).resolve().parents[2]
TOY_PRD = (REPO / "tests" / "fixtures" / "toy" / "prd.md").read_text()
HOOK_BIN = shutil.which("gauntlet-judge-hook") or str(
    REPO / ".venv" / "bin" / "gauntlet-judge-hook"
)

# Real frontier/strong/cheap profiles, pinned like the bootstrap's own config.
CONFIG = """\
base_branch: main
branch_prefix: "gauntlet/"
run_root: runs
test_command: "uv run pytest -q"
agents:
  builder:
    adapter: claude-code
    model: opus
    permission_mode: acceptEdits
    allowed_tools: [Bash, Read, Write, Edit, Grep, Glob]
    base_flags: ["--setting-sources", "project"]
    step_timeout_s: 3600
  reviewer: {adapter: codex, model: gpt-5.5, sandbox: read-only}
  triage: {adapter: api, model: gpt-5-mini}
  escalation: {adapter: api, model: gpt-5}
  judge_llm: {adapter: api, model: gpt-5-mini}
identities:
  builder: {name: "Gauntlet Builder (claude)", email: "builder@gauntlet.local"}
  reviewer: {name: "Gauntlet Reviewer (codex)", email: "reviewer@gauntlet.local"}
  triage: {name: "Gauntlet Triage", email: "triage@gauntlet.local"}
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _scaffold(tmp_path: Path) -> Path:
    """A scratch repo carrying the real assets + the human toy PRD (FR-10.1)."""
    repo = tmp_path / "toyrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@gauntlet.local")
    _git(repo, "config", "commit.gpgsign", "false")
    for d in ("schemas", "prompts"):
        shutil.copytree(REPO / d, repo / d)
    (repo / "pipelines").mkdir()
    shutil.copy2(REPO / "pipelines" / "standard.yaml", repo / "pipelines" / "standard.yaml")
    shutil.copy2(REPO / "policy.yaml", repo / "policy.yaml")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG)
    # a minimal uv project so `uv run pytest` works inside the phase loop
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'toy'\nversion = '0.0.0'\nrequires-python = '>=3.12'\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": HOOK_BIN, "timeout": 15}]}]}
    }))
    (repo / "runs" / "toy").mkdir(parents=True)
    (repo / "runs" / "toy" / "prd.md").write_text(TOY_PRD)  # human-authored (FR-10.1)
    (repo / "README.md").write_text("toy\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed toy project")
    _git(repo, "branch", "-M", "main")
    return repo


@pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("codex") is None,
    reason="standard end-to-end needs both claude and codex CLIs",
)
def test_standard_pipeline_end_to_end_on_toy_prd(tmp_path):
    repo = _scaffold(tmp_path)
    mgr = RunManager(repo)
    pipe = repo / "pipelines" / "standard.yaml"

    # PRD gate: the cycle reviews the human PRD, then parks for ratification.
    status = mgr.start("toy", pipe, use_judge=True)
    assert status == M.RUN_PARKED, mgr.status("toy").model_dump()
    assert mgr.status("toy").current_step == "prd-approve"

    # plan gate: builder authors plan.md (with its gauntlet-phases block), the
    # cycle reviews it, parks for ratification.
    status = mgr.approve("toy", use_judge=True)
    assert status == M.RUN_PARKED
    assert mgr.status("toy").current_step == "plan-approve"
    plan = (repo / "runs" / "toy" / "plan.md").read_text()
    assert "gauntlet-phases" in plan  # the structured phase list the loop fans over

    # phases → retro → done: each phase implements, tests, commits, reviews.
    status = mgr.approve("toy", use_judge=True)
    assert status == M.RUN_DONE, mgr.status("toy").model_dump()

    man = mgr.status("toy")
    # FR-9 history: PLAN baseline + at least one numbered phase commit.
    phases = [c.phase for c in man.commits]
    assert "PLAN" in phases
    assert any(p.split(".")[0].lstrip("P").isdigit() for p in phases)
    # the toy was actually implemented and its tests pass
    assert (repo / "slugify.py").exists()
    # FR-9 clean history: the final tree is committed — only the run's own
    # bookkeeping under runs/ is excluded. Asserted directly, not vacuously
    # (review F-006): a dirty worktree at run end is a hard failure.
    assert gitops.is_clean(repo, exclude=["runs"]), gitops.status_porcelain(
        repo, exclude=["runs"]
    )

    # FR-9.8: PR.md drafted, not opened/pushed.
    pr = repo / "runs" / "toy" / "PR.md"
    assert pr.exists() and "Not opened, not pushed" in pr.read_text()

    # FR-3 acceptance: classification (triage) is a small, measured share of
    # total cost. The triage row and its percentage MUST be present — a missing
    # row or null percentage fails the acceptance rather than passing vacuously
    # (review F-006).
    report = build_report(man)
    assert report.total_cost, (
        "run reported no priced total cost; FR-3 cost acceptance is unmeasurable"
    )
    tri = next((a for a in report.agents if a.agent == "triage"), None)
    assert tri is not None, "no triage cost row; classification spend not attributed"
    assert tri.pct_cost is not None, "triage percentage is null; cannot verify FR-3"
    assert tri.pct_cost < 5.0
