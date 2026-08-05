"""Deterministic policy engine — the judge's fast path (FR-7.2).

Rules are evaluated **deny-first**: any matching deny rule wins immediately,
before allow or ask rules are considered. A matched allow rule resolves to
``allow``; a matched ask rule resolves to ``ask`` (escalate to the LLM
classifier); no match returns ``None`` (also escalate). Only ``allow`` and
``deny`` are terminal fast-path outcomes.

Matchers operate on the hook payload ``{tool_name, tool_input}`` plus the run's
``repo_root``. Two matcher families:
- ``command_patterns``: regexes against the Bash command string (the primary
  surface FR-7.6 enumerates).
- structural path checks (``path_escape``, ``credential_path``): resolve the
  path a file tool (or a Bash command) touches and test it against the repo
  boundary — robust where regex on shell text is not.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from gauntlet.judge.decision import JudgeDecision

Action = Literal["deny", "allow", "ask"]

# Tools whose tool_input carries a filesystem path under a known key.
PATH_INPUT_KEYS = ("file_path", "path", "notebook_path")

# Credential-bearing path fragments (boundary-aware where it matters). A read
# of one of these *outside the repo* is denied (FR-7.6).
CREDENTIAL_PATH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"/\.ssh/id_",
        r"/\.ssh/.*\.pem$",
        r"/\.aws/credentials",
        r"/\.config/gcloud/",
        r"/\.netrc$",
        r"/\.npmrc$",
        r"/\.pypirc$",
        r"/\.docker/config\.json$",
        r"/\.kube/config$",
        r"\.pem$",
        r"\.p12$",
        r"id_rsa",
        r"id_ed25519",
    )
)

# Extract candidate absolute or home-relative paths from a shell command.
_PATH_TOKEN_RE = re.compile(r"(?<![\w/])(~|/)[^\s'\";|&]*")


def _expand_user(path: Path) -> tuple[Path, bool]:
    """``path.expanduser()`` that never raises. Returns ``(path, expanded_ok)``.

    ``Path.expanduser`` raises ``RuntimeError`` — not an ``OSError`` — whenever a
    leading ``~`` cannot be resolved to a home directory: ``~unknownuser/...``
    (no such account), or a bare ``~`` in a process whose ``HOME`` is unset and
    whose uid has no passwd entry. Both are reachable from untrusted input: any
    agent Bash command is scanned for ``~``-prefixed path tokens, so an
    unhandled raise here crashes ``/decide`` instead of returning allow/deny.

    On failure the ORIGINAL, un-expanded path is returned with ``False`` so
    callers stay conservative: regex matchers still see the literal
    ``~/.ssh/id_rsa`` text, and boundary checks treat the path as outside the
    repo (§2 fail closed).
    """
    try:
        return path.expanduser(), True
    except RuntimeError:
        return path, False


# --- verifier-boundary confinement (PR #59 review B1 / PRD §7 items 1, 2, 4) ---
# These apply ONLY to a step with an engine-registered boundary (the verifier's
# disposable copy) — never to builder/operator sessions. They live in code, not
# policy.yaml, because they key on per-step registration state a static rule
# table cannot express.
#
# Relative parent-dir tokens in a Bash command ("cd ..", "cat ../../secrets"):
# _PATH_TOKEN_RE harvests only absolute/~ paths, but inside a boundary the cwd
# is the copy, so a `..` token is a live escape route and must be resolved.
_RELATIVE_ESCAPE_RE = re.compile(r"(?<![\w/.])\.\.(?:/[^\s'\";|&]*)?")
# Network egress surface reachable from Bash. Inside a boundary the posture is
# DEFAULT-DENY (PRD §7 item 2) — no allowlist: known fetch/transfer binaries,
# git against a remote, package-manager installs, and any explicit URL.
_CONFINED_NETWORK_RE = re.compile(
    r"\b(curl|wget|nc|ncat|netcat|ssh|scp|sftp|ftp|telnet|rsync)\b"
    r"|\bgit\s+(clone|fetch|pull|push)\b"
    r"|\b(pip3?|uv)\s+(?:[^\s;|&]+\s+)*install\b"
    r"|\bnpm\s+(install|ci|update|exec)\b"
    r"|https?://",
    re.IGNORECASE,
)
# Ref/remote-mutating git. The disposable copy is a `git worktree` sharing the
# real repo's object store, refs, and remotes — a `git tag`/`branch`/`push`
# from inside the copy lands in (or publishes) the REAL repo's state, which the
# working-tree mutation guard cannot see. Read-only git (status/log/diff/show)
# stays available for probing the deliverable.
_CONFINED_GIT_REF_RE = re.compile(
    r"\bgit\b[^;|&\n]*\b(push|fetch|pull|remote|tag|branch|update-ref|reflog"
    r"|gc|prune|worktree|submodule)\b",
    re.IGNORECASE,
)

# Shell constructs that chain, substitute, or redirect — their presence means a
# single allow rule matching one segment cannot vouch for the whole line
# (review P2 F-001). Such lines are escalated to the LLM/fail-closed rung
# instead of being allowed. Deny rules still run first (deny-first), so a
# dangerous segment that matches a deny pattern is still blocked.
_CHAINING_RE = re.compile(r"[;&|\n`]|\$\(|\bxargs\b|(?<![0-9])>|<\(")


class PolicyRule(BaseModel):
    name: str
    description: str = ""
    applies_to_tools: list[str] | None = None
    command_patterns: list[str] = Field(default_factory=list)
    # Regexes over the call's RESOLVED candidate paths — the operation targets
    # (file-tool path keys, Bash path tokens), never a file's content strings,
    # so a file whose content merely mentions a protected path is not matched
    # (the BOOTSTRAP-NOTES #32 false-positive class command_patterns would hit
    # for file tools). Added for the governed-learning-asset write guard
    # (PR #59 review F-5).
    path_patterns: list[str] = Field(default_factory=list)
    path_escape: bool = False  # path resolves outside repo_root
    credential_path: bool = False  # path matches a credential pattern (any location)
    credential_outside_repo: bool = False  # credential pattern AND outside repo
    risk_category: str | None = None
    # When True, the rule matches ONLY while a gauntlet pipeline step is active
    # (the decide request carried a step_id). Lets the policy gate an action for
    # in-run agents — FR-9.8 defense-in-depth (the pipeline must not autonomously
    # push or open PRs) — while still allowing it in the operator's own
    # interactive sessions, where push/PR is ordinary directed workflow.
    pipeline_step_only: bool = False
    # --- policy-change governance markers (FR-7.4) --------------------------
    # A stable identifier + version + ratification flag for a rule that a feature
    # preflight verifies deterministically before acting (the pr_read_commands@v1
    # gate for review PR-mode: gauntlet.judge.preflight). They are absent on every
    # ordinary rule (id/version None, ratified False), so the existing policy loads
    # unchanged; only a governed, version-pinned rule sets them. `ratified` is
    # asserted by the human policy-change process when the rule is added — an agent
    # never sets it (CLAUDE.md §2: humans ratify, agents propose). `version` is a
    # rule-level pin (e.g. "v1"), distinct from the file-level Policy.version.
    id: str | None = None
    version: str | int | None = None
    ratified: bool = False

    @field_validator("command_patterns", "path_patterns")
    @classmethod
    def _compilable(cls, patterns: list[str]) -> list[str]:
        for pat in patterns:
            re.compile(pat)  # raises at load time on a bad regex
        return patterns

    def compiled(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.command_patterns]

    def compiled_paths(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.path_patterns]


class Policy(BaseModel):
    version: int
    deny: list[PolicyRule] = Field(default_factory=list)
    allow: list[PolicyRule] = Field(default_factory=list)
    ask: list[PolicyRule] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Policy:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class PolicyEngine:
    """Evaluates a :class:`Policy` against hook payloads, deny-first."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def evaluate(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        repo_root: Path,
        step_id: str | None = None,
    ) -> JudgeDecision | None:
        command = self._command_text(tool_name, tool_input)
        paths = self._candidate_paths(tool_name, tool_input, command)
        chained = bool(_CHAINING_RE.search(command))
        # A non-empty step_id means this call originates from inside a gauntlet
        # pipeline step (an in-run agent), not the operator's free session.
        in_pipeline_step = bool(step_id)

        # Deny-first: a single matching deny rule is terminal (FR-7.2). Allow
        # rules are skipped when the command chains/redirects (review F-001),
        # so a benign prefix cannot bless a dangerous trailing segment; such
        # lines fall through to ask/None -> LLM/fail-closed.
        for action, rules in (
            ("deny", self.policy.deny),
            ("allow", self.policy.allow),
            ("ask", self.policy.ask),
        ):
            if action == "allow" and chained:
                continue
            for rule in rules:
                if self._matches(
                    rule, tool_name, command, paths, repo_root, in_pipeline_step
                ):
                    return JudgeDecision(
                        decision=action,  # type: ignore[arg-type]
                        source="fast-path",
                        rationale=rule.description or rule.name,
                        risk_category=rule.risk_category,
                        matched_rule=rule.name,
                    )
        return None

    # -- verifier-boundary confinement (PR #59 review B1 / PRD §7) --------------

    def confinement_deny(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        boundary: Path,
    ) -> JudgeDecision | None:
        """The boundary-confinement rung for a step with a registered boundary.

        Returns a terminal ``deny`` when the call reaches outside the disposable
        copy, or ``None`` to fall through to the normal ladder (which then runs
        with the boundary as its effective root). Enforces, mechanically, the
        §7 items the prompt could previously only request: reads AND writes
        outside the copy are denied (items 1/4 — every candidate path,
        including Bash path tokens and relative ``..`` escapes, must resolve
        inside the boundary), network is default-deny with no allowlist
        (item 2), and ref/remote-mutating git is denied because the copy shares
        the real repo's refs. Deny decisions are never cached, so this
        re-evaluates on every call."""
        command = self._command_text(tool_name, tool_input)
        # Network and git-ref checks run BEFORE the path check: a URL's
        # `//host/path` also parses as an outside path token, and the network
        # rationale is the actionable one for the audit trail.
        if tool_name == "Bash":
            if _CONFINED_NETWORK_RE.search(command):
                return JudgeDecision(
                    decision="deny",
                    source="fast-path",
                    rationale=(
                        "verifier confinement: network egress is default-deny "
                        "inside the disposable copy — no allowlist (PRD §7 item 2)"
                    ),
                    risk_category="network",
                    matched_rule="verifier-boundary-network",
                )
            if _CONFINED_GIT_REF_RE.search(command):
                return JudgeDecision(
                    decision="deny",
                    source="fast-path",
                    rationale=(
                        "verifier confinement: the disposable copy is a git "
                        "worktree sharing the real repo's refs/remotes — "
                        "ref/remote-mutating git is denied inside it"
                    ),
                    risk_category="sandbox-escape",
                    matched_rule="verifier-boundary-git-refs",
                )
        paths = self._candidate_paths(tool_name, tool_input, command)
        if tool_name == "Bash" and isinstance(tool_input.get("command"), str):
            # relative parent-dir tokens are live escapes from the copy cwd
            for match in _RELATIVE_ESCAPE_RE.finditer(tool_input["command"]):
                paths.append(Path(match.group(0)))
        for p in paths:
            if self._escapes(p, boundary):
                return JudgeDecision(
                    decision="deny",
                    source="fast-path",
                    rationale=(
                        f"verifier confinement: path {p} resolves outside the "
                        f"disposable copy {boundary} — reads and writes outside "
                        "the copy are denied (PRD §7 items 1/4)"
                    ),
                    risk_category="sandbox-escape",
                    matched_rule="verifier-boundary-path",
                )
        return None

    # -- matching --------------------------------------------------------------

    def _matches(
        self,
        rule: PolicyRule,
        tool_name: str,
        command: str,
        paths: list[Path],
        repo_root: Path,
        in_pipeline_step: bool = False,
    ) -> bool:
        if rule.applies_to_tools is not None and tool_name not in rule.applies_to_tools:
            return False
        if rule.pipeline_step_only and not in_pipeline_step:
            return False
        # A rule with multiple matcher kinds requires ALL specified kinds to
        # match (AND), so e.g. credential_outside_repo is precise.
        checks: list[bool] = []
        if rule.command_patterns:
            checks.append(any(p.search(command) for p in rule.compiled()))
        if rule.path_patterns:
            # match against RESOLVED operation-target paths (relative paths
            # resolve against the run's repo_root) — never content strings
            compiled_paths = rule.compiled_paths()
            checks.append(any(
                pat.search(str(self._resolve(p, repo_root)[0]))
                for p in paths for pat in compiled_paths
            ))
        if rule.path_escape:
            checks.append(any(self._escapes(p, repo_root) for p in paths))
        if rule.credential_path:
            checks.append(any(self._is_credential(p) for p in paths))
        if rule.credential_outside_repo:
            checks.append(
                any(
                    self._is_credential(p) and self._escapes(p, repo_root)
                    for p in paths
                )
            )
        return bool(checks) and all(checks)

    @staticmethod
    def _command_text(tool_name: str, tool_input: dict[str, Any]) -> str:
        # Bash carries `command`; other tools get a flattened string so command
        # patterns can still match content/paths if a rule wants them to.
        if "command" in tool_input and isinstance(tool_input["command"], str):
            return tool_input["command"]
        parts: list[str] = [tool_name]
        for value in tool_input.values():
            if isinstance(value, str):
                parts.append(value)
        return " ".join(parts)

    def _candidate_paths(
        self, tool_name: str, tool_input: dict[str, Any], command: str
    ) -> list[Path]:
        paths: list[Path] = []
        for key in PATH_INPUT_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                paths.append(Path(value))
        # Harvest path-looking tokens ONLY from a real shell command (Bash),
        # where a path token IS an operation target. For structured file tools
        # (Edit/Write/...), the operation target is the explicit path key; the
        # other string fields are CONTENT (old_string/new_string), and a file
        # that legitimately contains a path string — pervasive in a
        # path-handling codebase — must not be judged as operating on that path
        # (BOOTSTRAP-NOTES #32: this false-positive denied in-repo edits to
        # files whose content mentions an absolute path, stalling P5).
        # Gate on the tool NAME, not merely the presence of a `command` key, so a
        # non-Bash tool that happens to carry a `command` string can't have its
        # content tokens harvested as operation targets (review).
        if tool_name == "Bash" and isinstance(tool_input.get("command"), str):
            for match in _PATH_TOKEN_RE.finditer(tool_input["command"]):
                paths.append(Path(match.group(0)))
        return paths

    @staticmethod
    def _escapes(path: Path, repo_root: Path) -> bool:
        # Resolve relative paths against the request's repo_root (FR-7.1 run
        # context), NOT the judge process cwd, and follow symlinks so a
        # symlinked escape is caught (review F-005). Both sides go through
        # realpath so a symlinked repo_root (e.g. macOS /tmp -> /private/tmp)
        # compares consistently.
        resolved, path_ok = PolicyEngine._resolve(path, repo_root)
        root_expanded, root_ok = _expand_user(repo_root)
        if not (path_ok and root_ok):
            # A `~` we cannot expand (unknown user, or no home for this process)
            # names a home directory we cannot place relative to the repo. Fail
            # closed (§2): treat it as outside the boundary rather than letting
            # an unresolvable path pass the escape check.
            return True
        root = Path(os.path.realpath(str(root_expanded)))
        try:
            resolved.relative_to(root)
            return False
        except ValueError:
            return True

    @staticmethod
    def _resolve(path: Path, base: Path) -> tuple[Path, bool]:
        """Resolve ``path`` against ``base``. Returns ``(resolved, expanded_ok)``.

        ``expanded_ok`` is False when a leading ``~`` could not be expanded; the
        returned path then carries the literal, un-expanded text so regex
        matchers still see ``~/.aws/credentials`` rather than nothing at all.
        Callers that need a boundary decision must fail closed on False.
        """
        expanded, ok = _expand_user(path)
        if not expanded.is_absolute():
            base_expanded, base_ok = _expand_user(base)
            ok = ok and base_ok
            expanded = base_expanded / expanded
        # realpath follows symlinks for the existing prefix and lexically
        # normalizes the rest (no existence requirement), so both `..` escapes
        # and symlink escapes resolve to their real target.
        return Path(os.path.realpath(str(expanded))), ok

    @staticmethod
    def _is_credential(path: Path) -> bool:
        # An un-expandable `~` falls back to the literal text, which still
        # carries the credential fragment (`~/.ssh/id_rsa`), so the match stays
        # conservative instead of crashing the decide endpoint (§2 fail closed).
        expanded, _ = _expand_user(path)
        text = str(expanded)
        return any(p.search(text) for p in CREDENTIAL_PATH_PATTERNS)
