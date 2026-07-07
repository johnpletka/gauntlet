"""P5 — verifier sandbox contract, real-backend enforcement (FR-2.5, §7).

Marked ``integration`` (CI runs ``-m "not integration"``; the operator runs these
locally). The v1 backend is claude-code + the engine-managed judge PreToolUse
hook, so a genuine enforcement test needs the claude CLI *and* a live judge — a
heavy fixture. These tests exercise what is feasible on a real host and skip
cleanly otherwise; the deterministic mechanism coverage (env stripping, the
copy-pointed judge boundary, the withheld network tools, the resource-limit park,
the fail-closed probes/parks) lives in the unit suite (``tests/unit/test_verify.py``,
``tests/unit/test_acceptance_gate.py``) and is what the P5 acceptance map cites.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import gitops, verify

pytestmark = [pytest.mark.integration]

REPO = Path(__file__).resolve().parents[2]


def _seed_repo(tmp_path):
    """A tiny git repo whose 'working feature' add() actually SUBTRACTS — a
    behavioral bug that looks correct on a diff but is wrong when executed."""
    repo = tmp_path / "seed"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "calc.py").write_text(
        "def add(a, b):\n"
        "    # looks like addition; actually subtracts (behavioral bug)\n"
        "    return a - b\n\n"
        "if __name__ == '__main__':\n"
        "    print(add(2, 3))\n"
    )
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "seed"], cwd=repo, check=True)
    return repo


def test_disposable_copy_is_faithful_and_leaves_real_tree_unchanged(tmp_path):
    """FR-2.1/P5-A4: a disposable copy is a faithful checkout of HEAD, and creating
    + discarding it leaves the real worktree's tree hash unchanged."""
    repo = _seed_repo(tmp_path)
    before = gitops.worktree_tree_hash(repo)
    copy = verify.make_disposable_copy(repo)
    try:
        assert (copy.path / "calc.py").read_text() == (repo / "calc.py").read_text()
        # mutate the copy freely — the real tree must not move
        (copy.path / "calc.py").write_text("mutated in the disposable copy\n")
    finally:
        verify.discard_disposable_copy(repo, copy)
    assert gitops.worktree_tree_hash(repo) == before
    assert gitops.is_clean(repo)


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_verifier_process_env_has_no_secrets(tmp_path, monkeypatch):
    """§7 env-stripping (P5-A3), process-level: a claude-code verifier spawned with
    the rebuilt env has no secret var in its actual process environment — verified
    by having the sandboxed Bash tool print `env`. Needs a live judge+model, so it
    skips when the verifier cannot run; the deterministic env-strip check is
    ``test_build_sandbox_env_keeps_allowlist_strips_secrets`` (unit)."""
    from gauntlet.adapters.base import AdapterError
    from gauntlet.adapters.claude_code import ClaudeCodeAdapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_DEPLOY_TOKEN", "tok-should-not-leak")
    env = verify.build_sandbox_env()
    assert "ANTHROPIC_API_KEY" not in env and "MY_DEPLOY_TOKEN" not in env
    if not os.environ.get(verify.TOKEN_ENV_VAR):
        pytest.skip("no active judge for a hooked verifier run")
    repo = _seed_repo(tmp_path)
    copy = verify.make_disposable_copy(repo)
    adapter = ClaudeCodeAdapter(model="opus", timeout_s=120.0)
    verify.configure_claude_verifier(
        adapter, env=verify.verifier_env(dict(os.environ), copy.path))
    try:
        result = adapter.run(
            "Run `env` with the Bash tool and report its full output verbatim.",
            cwd=copy.path)
        assert "sk-should-not-leak" not in result.text
        assert "tok-should-not-leak" not in result.text
    except AdapterError as exc:
        pytest.skip(f"claude verifier unavailable: {exc}")
    finally:
        verify.discard_disposable_copy(repo, copy)


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_seeded_behavioral_bug_yields_finding(tmp_path):
    """P5-A1: the verifier agent, run over a fixture with a runtime bug (correct-
    looking code, wrong behavior), yields ≥1 behavioral finding with executed-
    command evidence. Needs a live judge+model, so it skips when unavailable rather
    than failing the suite."""
    from gauntlet.adapters.base import AdapterError
    from gauntlet.adapters.claude_code import ClaudeCodeAdapter

    if not os.environ.get(verify.TOKEN_ENV_VAR):
        pytest.skip("no active judge for a hooked verifier run")
    repo = _seed_repo(tmp_path)
    schema = json.loads((REPO / "schemas" / "findings.json").read_text())
    copy = verify.make_disposable_copy(repo)
    adapter = ClaudeCodeAdapter(model="opus", timeout_s=300.0)
    verify.configure_claude_verifier(
        adapter, env=verify.verifier_env(dict(os.environ), copy.path))
    prompt = (
        "You have a sandboxed copy of a tiny project. Run `python3 calc.py` and "
        "reason about whether add(2, 3) behaves correctly. If the runtime output "
        "is wrong, return a findings-schema JSON object with one finding whose "
        "category is `behavioral` and whose evidence contains the exact command "
        "you ran and its output. If correct, return an empty findings list."
    )
    try:
        result = adapter.run(prompt, schema=schema, cwd=copy.path)
    except AdapterError as exc:
        pytest.skip(f"claude verifier unavailable: {exc}")
    finally:
        verify.discard_disposable_copy(repo, copy)
    findings = (result.structured or {}).get("findings") or []
    behavioral = [f for f in findings if f.get("category") == "behavioral"]
    assert behavioral, f"verifier raised no behavioral finding: {result.text[:400]}"
    assert behavioral[0].get("evidence"), "behavioral finding carries no command evidence"
