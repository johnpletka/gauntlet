"""FR-7.4 deterministic preflight for the `pr_read_commands@v1` policy rule.

The reader is a pure config read: no network, no agent. These tests pin the four
outcomes against fixtures (ratified -> ok; absent / unratified / version-mismatch
-> fail closed with the *exact* FR-7.4 message), the missing-file fail-closed
path, and the current repo policy (rule not yet ratified => absent). A final test
proves the P4 rule-proposal artifact is well-formed policy YAML carrying the
id/version/ratified fields the reader checks — and that feeding it to the reader
returns ok.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml

from gauntlet.judge.policy import Policy, PolicyRule
from gauntlet.judge.preflight import (
    ABSENT,
    RULE_ID,
    RULE_VERSION,
    UNRATIFIED,
    VERSION_MISMATCH,
    check_pr_read_commands,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROPOSAL = (
    REPO_ROOT
    / "runs"
    / "lightweight-issue-workflow"
    / "proposals"
    / "pr-read-commands.md"
)

# The exact FR-7.4 messages, hardcoded here (NOT derived from the reader's own
# helper) so a regression in the message construction is caught, not masked.
_ABSENT_MSG = (
    "P4 (PR mode) requires policy rule 'pr_read_commands@v1' to be ratified in "
    "policy.yaml; it is absent. Ratify it through the policy-change process "
    "(Open Question 11.4) before using --pr."
)
_UNRATIFIED_MSG = (
    "P4 (PR mode) requires policy rule 'pr_read_commands@v1' to be ratified in "
    "policy.yaml; it is unratified. Ratify it through the policy-change process "
    "(Open Question 11.4) before using --pr."
)
_VERSION_V2_MSG = (
    "P4 (PR mode) requires policy rule 'pr_read_commands@v1' to be ratified in "
    "policy.yaml; it is version v2 != v1. Ratify it through the policy-change "
    "process (Open Question 11.4) before using --pr."
)


def _write_policy(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(textwrap.dedent(body))
    return path


# A minimal ratified rule fixture, and the same rule with each field perturbed.
def _rule_yaml(*, ratified: bool = True, version: str = "v1") -> str:
    return textwrap.dedent(
        f"""\
        version: 1
        allow:
          - name: pr-read-commands
            id: pr_read_commands
            version: {version}
            ratified: {"true" if ratified else "false"}
            description: "PR-mode reads."
            applies_to_tools: [Bash]
            command_patterns:
              - '^\\s*gh\\s+pr\\s+(view|checkout)\\b'
              - '^\\s*git\\s+fetch\\b'
        """
    )


def test_ratified_rule_passes(tmp_path):
    result = check_pr_read_commands(_write_policy(tmp_path, _rule_yaml()))
    assert result.ok
    assert result.reason == "ratified"
    assert result.message is None


def test_absent_rule_fails_closed(tmp_path):
    # A well-formed policy that simply lacks the rule => absent.
    path = _write_policy(
        tmp_path,
        """\
        version: 1
        allow:
          - name: git-readonly
            applies_to_tools: [Bash]
            command_patterns:
              - '^\\s*git\\s+status\\b'
        """,
    )
    result = check_pr_read_commands(path)
    assert not result.ok
    assert result.reason == ABSENT
    assert result.message == _ABSENT_MSG


def test_unratified_rule_fails_closed(tmp_path):
    path = _write_policy(tmp_path, _rule_yaml(ratified=False))
    result = check_pr_read_commands(path)
    assert not result.ok
    assert result.reason == UNRATIFIED
    assert result.message == _UNRATIFIED_MSG


def test_version_mismatch_fails_closed(tmp_path):
    path = _write_policy(tmp_path, _rule_yaml(version="v2"))
    result = check_pr_read_commands(path)
    assert not result.ok
    assert result.reason == VERSION_MISMATCH
    assert result.found_version == "v2"
    assert result.message == _VERSION_V2_MSG


def test_unratified_precedes_version_mismatch(tmp_path):
    # A rule that is BOTH unratified and wrong-version reports unratified first
    # (FR-7.4 precedence: absent > unratified > version-mismatch).
    path = _write_policy(tmp_path, _rule_yaml(ratified=False, version="v2"))
    result = check_pr_read_commands(path)
    assert result.reason == UNRATIFIED


def test_missing_policy_file_is_absent(tmp_path):
    # No policy at all: the ratified rule cannot be present => fail closed absent.
    result = check_pr_read_commands(tmp_path / "does-not-exist.yaml")
    assert not result.ok
    assert result.reason == ABSENT
    assert result.message == _ABSENT_MSG


def _misbucketed_policy(tmp_path: Path, bucket: str) -> Path:
    # The governed id, ratified at v1, but parked under a non-allow bucket.
    return _write_policy(
        tmp_path,
        f"""\
        version: 1
        {bucket}:
          - name: pr-read-commands
            id: pr_read_commands
            version: v1
            ratified: true
            applies_to_tools: [Bash]
            command_patterns:
              - '^\\s*gh\\s+pr\\s+(view|checkout)\\b'
        """,
    )


def test_ratified_rule_under_deny_fails_closed(tmp_path):
    # The judge boundary FR-7.3/FR-7.4 require is an *allow* rule. A ratified v1
    # rule parked under deny does NOT establish it: the preflight scopes its
    # lookup to allow and reads a misbucketed id as absent (fails closed).
    result = check_pr_read_commands(_misbucketed_policy(tmp_path, "deny"))
    assert not result.ok
    assert result.reason == ABSENT
    assert result.message == _ABSENT_MSG


def test_ratified_rule_under_ask_fails_closed(tmp_path):
    # Same fail-closed behavior for the id parked under ask: only an allow rule
    # counts, so this reads as absent.
    result = check_pr_read_commands(_misbucketed_policy(tmp_path, "ask"))
    assert not result.ok
    assert result.reason == ABSENT
    assert result.message == _ABSENT_MSG


def test_current_repo_policy_lacks_the_rule():
    # The rule is ratified out of band (P4->P5 gate); until then the live policy
    # correctly reports it absent — the intended fail-closed state.
    result = check_pr_read_commands(REPO_ROOT / "policy.yaml")
    assert not result.ok
    assert result.reason == ABSENT


def test_existing_rules_load_without_governance_fields():
    # The new optional fields must not break loading the real repo policy: every
    # ordinary rule defaults id/version None, ratified False.
    policy = Policy.load(REPO_ROOT / "policy.yaml")
    for rule in policy.allow + policy.deny + policy.ask:
        assert rule.id is None
        assert rule.version is None
        assert rule.ratified is False


# --- the P4 rule-proposal artifact --------------------------------------------

_FENCED_YAML_RE = re.compile(r"```yaml\n(.*?)\n```", re.DOTALL)


def _proposal_rule_block() -> dict:
    text = PROPOSAL.read_text()
    m = _FENCED_YAML_RE.search(text)
    assert m, "the proposal must contain a ```yaml``` fenced rule block"
    block = yaml.safe_load(m.group(1))
    assert isinstance(block, dict) and "allow" in block
    return block


def test_proposal_artifact_exists():
    assert PROPOSAL.is_file(), (
        "P4 must author the proposal under the run dir: "
        "runs/lightweight-issue-workflow/proposals/pr-read-commands.md"
    )


def test_proposal_rule_is_well_formed_and_carries_governance_fields():
    block = _proposal_rule_block()
    rules = [PolicyRule.model_validate(r) for r in block["allow"]]
    rule = next(r for r in rules if r.id == RULE_ID)
    assert str(rule.version) == RULE_VERSION
    assert rule.ratified is True
    assert rule.name == "pr-read-commands"
    # It blesses the three PR-mode read commands.
    joined = " ".join(rule.command_patterns)
    assert "gh" in joined and "pr" in joined and "checkout" in joined
    assert "git" in joined and "fetch" in joined


def test_proposal_rule_satisfies_the_preflight(tmp_path):
    # End-to-end: the rule in the proposal, dropped into a policy.yaml, makes the
    # reader return ok — so ratifying it exactly as written clears the gate.
    block = _proposal_rule_block()
    policy_dict = {"version": 1, **block}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy_dict, sort_keys=False))
    assert check_pr_read_commands(path).ok
