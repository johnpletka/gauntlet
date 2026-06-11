"""Configuration validation primitives.

P1 ships the safety lint (PRD §8): permission-bypass and hook-disabling flags
are rejected at config/command-build time, not merely avoided by convention.
The full agent-profile config model (`.gauntlet/config.yaml`, FR-2.1) lands
in P3; adapters call :func:`lint_flags` on every command line they build.
"""

from __future__ import annotations

from collections.abc import Sequence


class BannedFlagError(ValueError):
    """A command line contains a permission-bypass or hook-disabling flag."""


# Flags that disable the permission system or the hook safety layer (PRD §8,
# FR-7.3). Banned as exact tokens and in `--flag=value` form.
BANNED_FLAGS: frozenset[str] = frozenset(
    {
        # claude: full permission bypass disables PreToolUse enforcement
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        # claude: --bare skips hooks entirely (verified in `claude --help` 2.1.172)
        "--bare",
        # codex: bypasses both approvals and the sandbox
        "--dangerously-bypass-approvals-and-sandbox",
        "--yolo",
        # codex: runs hooks without trust verification
        "--dangerously-bypass-hook-trust",
    }
)

# Flags whose *value* can amount to a bypass even though the flag is fine.
BANNED_FLAG_VALUES: dict[str, frozenset[str]] = {
    "--permission-mode": frozenset({"bypassPermissions"}),
    "--sandbox": frozenset({"danger-full-access"}),
    "-s": frozenset({"danger-full-access"}),
}

# codex config overrides (`-c key=value`) that bypass the sandbox the same
# way the banned flag values do (review P1 F-003: the adapter itself uses
# `-c sandbox_mode=...` on resume, so this channel is a live control path).
BANNED_CONFIG_VALUES: dict[str, frozenset[str]] = {
    "sandbox_mode": frozenset({"danger-full-access"}),
}


def lint_flags(argv: Sequence[str]) -> None:
    """Reject permission-bypass / hook-disabling flags anywhere in ``argv``.

    Raises :class:`BannedFlagError` on the first violation. Checks bare
    tokens, ``--flag=value`` forms, and value-position bypasses such as
    ``--permission-mode bypassPermissions``.
    """
    tokens = list(argv)
    for i, token in enumerate(tokens):
        flag, eq, value = token.partition("=")
        if flag in BANNED_FLAGS:
            raise BannedFlagError(
                f"banned flag {flag!r}: permission-bypass/hook-disabling flags "
                "are rejected (PRD §8); they disable the safety layer"
            )
        if flag in BANNED_FLAG_VALUES:
            effective = value if eq else (tokens[i + 1] if i + 1 < len(tokens) else "")
            if effective in BANNED_FLAG_VALUES[flag]:
                raise BannedFlagError(
                    f"banned value {effective!r} for {flag!r}: amounts to a "
                    "permission/sandbox bypass (PRD §8)"
                )
        if flag in ("-c", "--config"):
            assignment = value if eq else (tokens[i + 1] if i + 1 < len(tokens) else "")
            _check_config_assignment(assignment)


def _check_config_assignment(assignment: str) -> None:
    """Reject codex `-c key=value` overrides that bypass the sandbox."""
    key, eq, value = assignment.partition("=")
    if not eq:
        return
    key = key.strip()
    value = value.strip().strip("\"'")  # TOML values arrive quoted or bare
    if key in BANNED_CONFIG_VALUES and value in BANNED_CONFIG_VALUES[key]:
        raise BannedFlagError(
            f"banned config override {key}={value!r}: amounts to a sandbox "
            "bypass (PRD §8)"
        )
