"""Full `standard.yaml` end-to-end with scripted fakes (P5).

Drives the whole 3-gate workflow through `RunManager` — prd-cycle → prd-approve
→ plan-author → plan-cycle → plan-approve → foreach plan.phases [implement →
tests → phase-commit → impl-cycle] → retro → DONE — with fake adapters injected
by profile name, so the loop is exercised offline (no creds). The live-CLI run
on a real toy PRD lives in tests/integration (FR-10.1 toy run).

Also covers the FR-5.3/5.4 acceptance: add a review step to one stage by YAML
edit ONLY, and show it validates, runs, and appears in the manifest/report.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

from gauntlet.adapters.base import AdapterCapabilities, AgentResult, Usage
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.report import build_report
from gauntlet.engine.run import RunManager

REPO = Path(__file__).resolve().parents[2]

PLAN_MD = """# Toy implementation plan

One phase: build the widget.

```gauntlet-phases
- id: P1
  title: Build the widget
  goal: Implement widget(); validates the toy spec is satisfiable end-to-end.
```
"""

TOY_PRD = """# PRD: Toy widget

A tiny, human-authored toy spec used to exercise the full Gauntlet loop.

## Problem statement
We need a `widget()` function that returns the string "widget".

## Requirements
- FR-1: `widget()` returns "widget".
"""

CFG = """\
base_branch: main
branch_prefix: "gauntlet/"
run_root: runs
test_command: "true"
agents:
  builder: {adapter: claude-code, permission_mode: acceptEdits}
  reviewer: {adapter: codex, sandbox: read-only}
  triage: {adapter: api, model: gpt-5-mini}
  escalation: {adapter: api, model: gpt-5}
identities:
  builder: {name: "Gauntlet Builder (claude)", email: "builder@gauntlet.local"}
  reviewer: {name: "Gauntlet Reviewer (codex)", email: "reviewer@gauntlet.local"}
  triage: {name: "Gauntlet Triage", email: "triage@gauntlet.local"}
"""


class Script:
    """A scripted adapter: each call pops the next response (an AgentResult or a
    callable(cwd) -> AgentResult)."""

    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.timeout_s = 600.0

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.calls.append({"prompt": prompt, "schema": schema})
        assert self.responses, "Script exhausted; unexpected extra call"
        r = self.responses.pop(0)
        return r(cwd) if callable(r) else r


def _u():
    return Usage(input_tokens=10, output_tokens=5)


def review_empty():
    body = {"findings": [], "open_questions": [], "summary": "looks good"}
    return AgentResult(text="{}", structured=body, usage=_u(), exit_code=0)


def text_result(text, writes=None):
    def _run(cwd):
        for rel, content in (writes or {}).items():
            (Path(cwd) / rel).write_text(content)
        return AgentResult(text=text, usage=_u(), exit_code=0)
    return _run


def _scaffold(tmp_path: Path, *, pipeline_text: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True, text=True)

    g("init", "-q")
    g("config", "user.name", "Fixture")
    g("config", "user.email", "fixture@gauntlet.local")
    g("config", "commit.gpgsign", "false")
    # the engine + cycle read schemas/ and prompts/ from the repo root
    shutil.copytree(REPO / "schemas", repo / "schemas")
    shutil.copytree(REPO / "prompts", repo / "prompts")
    (repo / "pipelines").mkdir()
    pipe = pipeline_text or (REPO / "pipelines" / "standard.yaml").read_text()
    (repo / "pipelines" / "standard.yaml").write_text(pipe)
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CFG)
    (repo / "runs" / "toy").mkdir(parents=True)
    (repo / "runs" / "toy" / "prd.md").write_text(TOY_PRD)
    (repo / "README.md").write_text("toy\n")
    g("add", "-A")
    g("commit", "-qm", "seed scaffold")
    g("branch", "-M", "main")
    return repo


def _factory(adapters):
    return lambda name: adapters[name]


def test_standard_runs_end_to_end_with_fakes(tmp_path):
    repo = _scaffold(tmp_path)
    adapters = {
        # prd-cycle, plan-cycle, impl-cycle each converge on an empty review
        "reviewer": Script(review_empty(), review_empty(), review_empty()),
        "builder": Script(
            text_result(PLAN_MD),                              # plan-author
            text_result("implemented P1", {"widget.py": "def widget(): return 'widget'\n"}),
        ),
        "triage": Script(
            AgentResult(text="P1: build the widget\n\nImplements widget() for P1.\n"
                             "Validates the toy spec end-to-end.\n",
                        usage=_u(), exit_code=0),                # phase-commit draft
        ),
        "escalation": Script(),
    }
    mgr = RunManager(repo)
    pipe = repo / "pipelines" / "standard.yaml"

    # PRD gate
    status = mgr.start("toy", pipe, use_judge=False, adapter_factory=_factory(adapters))
    assert status == M.RUN_PARKED
    assert mgr.status("toy").current_step == "prd-approve"

    # plan gate
    status = mgr.approve("toy", use_judge=False, adapter_factory=_factory(adapters))
    assert status == M.RUN_PARKED
    assert mgr.status("toy").current_step == "plan-approve"

    # phases → retro → done
    status = mgr.approve("toy", use_judge=False, adapter_factory=_factory(adapters))
    assert status == M.RUN_DONE

    man = mgr.status("toy")
    # the plan baseline (PLAN), the phase commit (P1) — both labelled per #28/FR-5.1
    phases = [c.phase for c in man.commits]
    assert "PLAN" in phases
    assert "P1" in phases
    # branch holds the work
    assert man.branch == "gauntlet/toy"
    assert gitops.commit_subject(repo, "HEAD") in (
        "P1: build the widget", "PLAN: Author plan.md for adversarial review",
    )
    # every cycle converged; the phase implemented the widget
    assert (repo / "widget.py").exists()
    # the structured phase list drove exactly one iteration
    assert man.record("implement", "0").status == M.DONE
    assert man.record("retrospective").status == M.DONE

    # PR.md drafted at the final gate (FR-9.8), in the slug dir, not auto-committed
    pr = repo / "runs" / "toy" / "PR.md"
    assert pr.exists()
    assert "Toy widget" in pr.read_text()
    assert "Not opened, not pushed" in pr.read_text()

    # cost report attributes per profile (FR-3.2); classification (triage) tiny
    report = build_report(man)
    agents = {a.agent for a in report.agents}
    assert {"reviewer", "builder", "triage"} <= agents

    # the manifest pins the exact version of the WHOLE prompt set used (FR-5.6 /
    # the "versioned prompt set" deliverable) — including the cycle's review
    # variant overrides, not just `prompt:` templates.
    hashed = set(man.prompt_hashes)
    assert "prompts/plan-author.md" in hashed
    assert "prompts/review-document.md" in hashed
    assert "prompts/review-code.md" in hashed
    assert all(v.startswith("sha256:") for v in man.prompt_hashes.values())


def test_yaml_only_extension_adds_a_third_review_step(tmp_path):
    # FR-5.3/5.4 acceptance: add a review step to one stage by EDITING YAML only;
    # it validates, runs, and shows up in the manifest + cost report. No code.
    spec = yaml.safe_load((REPO / "pipelines" / "standard.yaml").read_text())
    phases = next(s for s in spec["stages"] if s["id"] == "phases")
    extra = {
        "id": "impl-cycle-2", "type": "adversarial_cycle", "mode": "code_review",
        "reviewer": "reviewer", "triager": "triage", "fixer": "builder",
        "escalation_agent": "escalation", "max_rounds": 2,
        "review_prompt": "prompts/review-code.md",
    }
    phases["steps"].append(extra)  # a second-pass code review on the same stage
    repo = _scaffold(tmp_path, pipeline_text=yaml.safe_dump(spec, sort_keys=False))

    adapters = {
        # one extra review call for the added step (4 cycles converge empty)
        "reviewer": Script(*[review_empty() for _ in range(4)]),
        "builder": Script(
            text_result(PLAN_MD),
            text_result("did P1", {"widget.py": "def widget(): return 'widget'\n"}),
        ),
        "triage": Script(
            AgentResult(text="P1: build widget\n\nImplements widget() for P1.\nbody.\n",
                        usage=_u(), exit_code=0),
        ),
        "escalation": Script(),
    }
    mgr = RunManager(repo)
    pipe = repo / "pipelines" / "standard.yaml"
    assert mgr.start("toy", pipe, use_judge=False, adapter_factory=_factory(adapters)) == M.RUN_PARKED
    assert mgr.approve("toy", use_judge=False, adapter_factory=_factory(adapters)) == M.RUN_PARKED
    assert mgr.approve("toy", use_judge=False, adapter_factory=_factory(adapters)) == M.RUN_DONE

    man = mgr.status("toy")
    # the YAML-only step ran and is in the manifest (transcripts + report follow)
    assert man.record("impl-cycle-2", "0") is not None
    assert man.record("impl-cycle-2", "0").status == M.DONE
    run_dir = (repo / "runs" / "toy" / man.run_id)
    assert (run_dir / "steps" / "impl-cycle-2.0" / "r1-review" / "prompt.md").exists()
