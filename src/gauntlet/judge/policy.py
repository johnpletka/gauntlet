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


class PolicyRule(BaseModel):
    name: str
    description: str = ""
    applies_to_tools: list[str] | None = None
    command_patterns: list[str] = Field(default_factory=list)
    path_escape: bool = False  # path resolves outside repo_root
    credential_path: bool = False  # path matches a credential pattern (any location)
    credential_outside_repo: bool = False  # credential pattern AND outside repo
    risk_category: str | None = None

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
    ) -> JudgeDecision | None:
        command = self._command_text(tool_name, tool_input)
        paths = self._candidate_paths(tool_name, tool_input, command)

        # Deny-first: a single matching deny rule is terminal (FR-7.2).
        for action, rules in (
            ("deny", self.policy.deny),
            ("allow", self.policy.allow),
            ("ask", self.policy.ask),
        ):
            for rule in rules:
                if self._matches(rule, tool_name, command, paths, repo_root):
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
    ) -> bool:
        if rule.applies_to_tools is not None and tool_name not in rule.applies_to_tools:
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
        # For Bash, harvest path-looking tokens (absolute or ~-relative).
        for match in _PATH_TOKEN_RE.finditer(command):
            paths.append(Path(match.group(0)))
        return paths

    @staticmethod
    def _escapes(path: Path, repo_root: Path) -> bool:
        resolved = PolicyEngine._resolve(path)
        root = repo_root.expanduser()
        try:
            root = root.resolve()
        except OSError:
            root = root.absolute()
        try:
            resolved.relative_to(root)
            return False
        except ValueError:
            return True

    @staticmethod
    def _resolve(path: Path) -> Path:
        expanded = path.expanduser()
        # Resolve without requiring existence; collapse .. lexically so a
        # repo-relative ../../etc/passwd is correctly seen as an escape.
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        # os.path.normpath via Path: use as_posix normalization
        parts: list[str] = []
        for part in expanded.parts:
            if part == "..":
                if parts and parts[-1] not in ("/", ""):
                    parts.pop()
            elif part != ".":
                parts.append(part)
        return Path(*parts) if parts else Path("/")

    @staticmethod
    def _is_credential(path: Path) -> bool:
        text = str(path.expanduser())
        return any(p.search(text) for p in CREDENTIAL_PATH_PATTERNS)
