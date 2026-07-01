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

    @field_validator("command_patterns")
    @classmethod
    def _compilable(cls, patterns: list[str]) -> list[str]:
        for pat in patterns:
            re.compile(pat)  # raises at load time on a bad regex
        return patterns

    def compiled(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.command_patterns]


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
        resolved = PolicyEngine._resolve(path, repo_root)
        root = Path(os.path.realpath(str(repo_root.expanduser())))
        try:
            resolved.relative_to(root)
            return False
        except ValueError:
            return True

    @staticmethod
    def _resolve(path: Path, base: Path) -> Path:
        expanded = path.expanduser()
        if not expanded.is_absolute():
            expanded = base.expanduser() / expanded
        # realpath follows symlinks for the existing prefix and lexically
        # normalizes the rest (no existence requirement), so both `..` escapes
        # and symlink escapes resolve to their real target.
        return Path(os.path.realpath(str(expanded)))

    @staticmethod
    def _is_credential(path: Path) -> bool:
        text = str(path.expanduser())
        return any(p.search(text) for p in CREDENTIAL_PATH_PATTERNS)
