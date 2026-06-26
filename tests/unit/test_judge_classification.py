"""Operator-vs-in-run judge classification proof (FR-10, validates §1.3 — P1).

The load-bearing belief: a foreground monitor wired to a run's judge with
``GAUNTLET_RUN_ID`` set but ``GAUNTLET_STEP_ID`` *unset* is classified by the
**unchanged** ``policy.yaml`` as the *operator's own session* (broad auto-allow,
no in-run denials), while a real in-run agent (``step_id`` present) still hits
its denials. The judge hook translates ``GAUNTLET_STEP_ID``'s presence into the
decide request's ``step_id`` field, so here "operator session" ≙ ``step_id``
**absent** and "in-run agent" ≙ ``step_id`` **present** — both with a valid
per-run ``run_id`` + token.

This asserts the judge's *decisions*, not env shape (P1's gate per PRD §8). Every
input is chosen to resolve on the policy fast path so the assertions are
deterministic (no LLM classifier consulted).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.judge.core import JudgeCore
from gauntlet.judge.policy import Policy, PolicyEngine
from gauntlet.judge.service import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = REPO_ROOT / "policy.yaml"
TOKEN = "per-run-judge-token"
RUN_ID = "run-2026-06-25T16-41-22"


@pytest.fixture
def client():
    # The unchanged policy.yaml (§2.2) — this proof never touches it.
    core = JudgeCore(PolicyEngine(Policy.load(POLICY)))
    return TestClient(create_app(core, token=TOKEN))


def _decide(client, command, *, step_id, token=TOKEN):
    body = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "repo_root": str(REPO_ROOT),
        "run_id": RUN_ID,
    }
    if step_id is not None:
        body["step_id"] = step_id
    return client.post("/decide", headers={"X-Gauntlet-Token": token}, json=body)


# The FR-10.1 verdict table. step_id absent → operator; "implement" → in-run.
# (command, operator_verdict, in_run_verdict)
_TABLE = [
    # The load-bearing rows: push / PR-open FLIP on step_id presence.
    ("git push origin feature-x", "allow", "deny"),
    ("gh pr create --fill", "allow", "deny"),
    # Read-only / test runners: unaffected by step scope — allow in both.
    ("git status", "allow", "allow"),
    ("uv run pytest", "allow", "allow"),
    # Ratification / history-rewrite: denied in EVERY context (operator is not a
    # blanket allow).
    ("gh pr merge 12", "deny", "deny"),
    ("git push --force origin feature-x", "deny", "deny"),
]


@pytest.mark.parametrize("command,operator,in_run", _TABLE)
def test_fr10_1_classification_table(client, command, operator, in_run):
    # Operator session: step_id ABSENT.
    op = _decide(client, command, step_id=None)
    assert op.status_code == 200
    op_body = op.json()
    assert op_body["decision"] == operator, f"operator {command!r}"
    assert op_body["source"] == "fast-path", f"{command!r} must resolve on the fast path"

    # In-run agent: same run_id, a non-empty step_id.
    inr = _decide(client, command, step_id="implement")
    assert inr.status_code == 200
    inr_body = inr.json()
    assert inr_body["decision"] == in_run, f"in-run {command!r}"
    assert inr_body["source"] == "fast-path", f"{command!r} must resolve on the fast path"


def test_fr10_1_push_and_pr_flip_on_step_id(client):
    # The two load-bearing rows, asserted explicitly: identical command + run_id,
    # the ONLY difference is step_id presence, and the verdict flips allow↔deny.
    for command, rule in (
        ("git push origin feature-x", "push-or-pr-open-in-pipeline-step"),
        ("gh pr create --fill", "push-or-pr-open-in-pipeline-step"),
    ):
        op = _decide(client, command, step_id=None).json()
        inr = _decide(client, command, step_id="implement").json()
        assert op["decision"] == "allow"
        assert inr["decision"] == "deny"
        assert inr["matched_rule"] == rule


def test_fr10_2_operator_shape_with_bad_token_is_rejected(client):
    # FR-10.2: authorization is still per-run. An operator-SHAPED request
    # (step_id absent) bearing a wrong token is rejected by the judge (401), NOT
    # auto-allowed — the classification never admits a non-operator caller.
    resp = _decide(client, "git push origin feature-x", step_id=None, token="wrong")
    assert resp.status_code == 401


def test_fr10_2_operator_shape_with_missing_token_is_rejected(client):
    body = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin feature-x"},
        "repo_root": str(REPO_ROOT),
        "run_id": RUN_ID,
    }
    resp = client.post("/decide", json=body)  # no X-Gauntlet-Token header
    assert resp.status_code == 401
