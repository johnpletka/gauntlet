"""Deterministic policy-rule preflight for review PR mode (FR-7.3/FR-7.4).

Before PR mode issues any ``gh pr view`` / ``gh pr checkout`` / ``git fetch``,
it must confirm the judge boundary for those reads is expressed as a ratified,
version-pinned allow rule in the active policy — **not** rely on the LLM
fallback. This module is that deterministic gate: it loads ``policy.yaml``, looks
up the ``pr_read_commands`` rule, and verifies it is present, marked ratified,
and at the expected version. No network, no agent — a pure config read, so the
check is the machine-checkable precondition FR-7.4 mandates rather than the
try-it-and-see anti-pattern the fail-closed principle forbids (CLAUDE.md §2).

The rule itself is authored as a proposal
(``runs/lightweight-issue-workflow/proposals/pr-read-commands.md``) and
ratified out of band through the policy-change process (CLAUDE.md §8, Open
Question 11.4) — this reader **never** edits ``policy.yaml``. PR mode (a later
phase) calls :func:`check_pr_read_commands` before any read command; on a not-ok
result it halts and escalates with the carried FR-7.4 message. Branch-mode
reviews issue none of these commands and skip the preflight entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gauntlet.judge.policy import Policy, PolicyRule

# The governed rule's stable identity (FR-7.4). The build expects exactly this
# id at exactly this version, both marked ratified.
RULE_ID = "pr_read_commands"
RULE_VERSION = "v1"
RULE_REF = f"{RULE_ID}@{RULE_VERSION}"

# Preflight outcome reasons, in the FR-7.4 precedence order (absent > unratified
# > version-mismatch). RATIFIED is the sole ok reason.
ABSENT = "absent"
UNRATIFIED = "unratified"
VERSION_MISMATCH = "version_mismatch"
RATIFIED = "ratified"


def _message(state: str) -> str:
    """The exact FR-7.4 fail-closed message for a not-ok ``state``.

    ``state`` is the substring the FR-7.4 template enumerates:
    ``"absent"``, ``"unratified"``, or ``"version <found> != v1"``.
    """
    return (
        f"P4 (PR mode) requires policy rule '{RULE_REF}' to be ratified in "
        f"policy.yaml; it is {state}. Ratify it through the policy-change process "
        "(Open Question 11.4) before using --pr."
    )


@dataclass(frozen=True)
class PreflightResult:
    """The outcome of the deterministic ``pr_read_commands@v1`` preflight (FR-7.4).

    ``ok`` is True only when the rule is present, ratified, and at
    :data:`RULE_VERSION`. Otherwise ``reason`` is one of :data:`ABSENT` /
    :data:`UNRATIFIED` / :data:`VERSION_MISMATCH` and ``message`` carries the
    exact FR-7.4 fail-closed text the caller escalates with. ``found_version``
    records the rule's actual version on a mismatch (else ``None``).
    """

    ok: bool
    reason: str
    message: str | None = None
    found_version: str | None = None


def _find_rule(policy: Policy) -> PolicyRule | None:
    """The ``allow`` rule carrying ``id == RULE_ID`` (searched only in ``allow``).

    The judge boundary FR-7.3/FR-7.4 require is an *allow* rule: it is what
    blesses ``gh pr view`` / ``gh pr checkout`` / ``git fetch`` on the
    deterministic fast path. A rule with the governed id parked under ``deny`` or
    ``ask`` does **not** establish that boundary, so the lookup is scoped to
    ``allow`` and a misbucketed id reads as :data:`ABSENT` — the preflight fails
    closed rather than treating a non-allow rule as sufficient."""
    for rule in policy.allow:
        if rule.id == RULE_ID:
            return rule
    return None


def check_pr_read_commands(policy_path: Path) -> PreflightResult:
    """Verify ``pr_read_commands@v1`` is present + ratified + versioned (FR-7.4).

    A deterministic config read — no network, no agent. Each of a
    missing/unreadable policy file, an absent rule, an unratified rule, or a
    version other than :data:`RULE_VERSION` fails closed (``ok=False``) with the
    exact FR-7.4 message for that state, in the order absent > unratified >
    version-mismatch. The ratified path returns ``ok=True`` with no message.

    A missing/unreadable ``policy.yaml`` maps to :data:`ABSENT`: the ratified
    rule cannot be present, and the absent message correctly points the operator
    at the policy-change process. A syntactically/structurally broken policy is a
    distinct operator error outside the three FR-7.4 states and is left to
    propagate (the judge itself would also refuse it) — the caller still fails
    closed on the raised error.
    """
    try:
        policy = Policy.load(policy_path)
    except OSError:
        return PreflightResult(ok=False, reason=ABSENT, message=_message(ABSENT))

    rule = _find_rule(policy)
    if rule is None:
        return PreflightResult(ok=False, reason=ABSENT, message=_message(ABSENT))
    if not rule.ratified:
        return PreflightResult(
            ok=False, reason=UNRATIFIED, message=_message(UNRATIFIED)
        )
    found = str(rule.version) if rule.version is not None else "none"
    if found != RULE_VERSION:
        return PreflightResult(
            ok=False,
            reason=VERSION_MISMATCH,
            message=_message(f"version {found} != {RULE_VERSION}"),
            found_version=found,
        )
    return PreflightResult(ok=True, reason=RATIFIED)
