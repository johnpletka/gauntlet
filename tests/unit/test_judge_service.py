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
