"""Judge HTTP service: token auth, /healthz, /decide framing (FR-7.1, §8)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.judge.core import JudgeCore
from gauntlet.judge.policy import Policy, PolicyEngine
from gauntlet.judge.service import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = REPO_ROOT / "policy.yaml"
TOKEN = "test-token-secret"


@pytest.fixture
def client():
    core = JudgeCore(PolicyEngine(Policy.load(POLICY)))
    return TestClient(create_app(core, token=TOKEN))


def test_healthz_unauthenticated(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_decide_requires_token(client):
    resp = client.post(
        "/decide",
        json={"tool_name": "Bash", "tool_input": {"command": "git status"}, "repo_root": str(REPO_ROOT)},
    )
    assert resp.status_code == 401


def test_decide_rejects_wrong_token(client):
    resp = client.post(
        "/decide",
        headers={"X-Gauntlet-Token": "wrong"},
        json={"tool_name": "Bash", "tool_input": {"command": "git status"}, "repo_root": str(REPO_ROOT)},
    )
    assert resp.status_code == 401


def test_decide_allows_benign(client):
    resp = client.post(
        "/decide",
        headers={"X-Gauntlet-Token": TOKEN},
        json={"tool_name": "Bash", "tool_input": {"command": "git status"}, "repo_root": str(REPO_ROOT)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "allow"
    assert body["source"] == "fast-path"


def test_decide_denies_dangerous(client):
    resp = client.post(
        "/decide",
        headers={"X-Gauntlet-Token": TOKEN},
        json={"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "repo_root": str(REPO_ROOT)},
    )
    assert resp.json()["decision"] == "deny"


def test_decide_fails_closed_unmatched_no_classifier(client):
    resp = client.post(
        "/decide",
        headers={"X-Gauntlet-Token": TOKEN},
        json={"tool_name": "Bash", "tool_input": {"command": "telnet x"}, "repo_root": str(REPO_ROOT)},
    )
    body = resp.json()
    assert body["decision"] == "deny"
    assert body["source"] == "fail-closed"


# --- /observed: verifier hook-loading probe readout (review F-001) --------------
@pytest.fixture
def bound_client():
    """A run-bound judge client (expected_run_id set), so /observed exercises the
    per-run authorization the verifier probe relies on."""
    core = JudgeCore(PolicyEngine(Policy.load(POLICY)))
    return TestClient(create_app(core, token=TOKEN, expected_run_id="r1"))


def _drive_probe_decide(client, nonce):
    from gauntlet.judge.hook_client import PROBE_STEP_PREFIX

    return client.post(
        "/decide",
        headers={"X-Gauntlet-Token": TOKEN},
        json={
            "tool_name": "Bash", "tool_input": {"command": f"echo {nonce}"},
            "repo_root": str(REPO_ROOT), "run_id": "r1",
            "step_id": f"{PROBE_STEP_PREFIX}{nonce}",
        },
    )


def test_observed_reports_seen_and_unseen_probe(bound_client):
    assert _drive_probe_decide(bound_client, "n1").status_code == 200
    seen = bound_client.get(
        "/observed", params={"run_id": "r1", "nonce": "n1"},
        headers={"X-Gauntlet-Token": TOKEN})
    assert seen.status_code == 200 and seen.json()["observed"] is True
    # a nonce the judge never saw is not observed
    unseen = bound_client.get(
        "/observed", params={"run_id": "r1", "nonce": "other"},
        headers={"X-Gauntlet-Token": TOKEN})
    assert unseen.json()["observed"] is False
    # an empty nonce is never observed
    empty = bound_client.get(
        "/observed", params={"run_id": "r1", "nonce": ""},
        headers={"X-Gauntlet-Token": TOKEN})
    assert empty.json()["observed"] is False


def test_observed_requires_token(bound_client):
    resp = bound_client.get("/observed", params={"run_id": "r1", "nonce": "n1"})
    assert resp.status_code == 401


def test_observed_rejects_wrong_run(bound_client):
    _drive_probe_decide(bound_client, "n1")
    resp = bound_client.get(
        "/observed", params={"run_id": "other-run", "nonce": "n1"},
        headers={"X-Gauntlet-Token": TOKEN})
    assert resp.status_code == 403
