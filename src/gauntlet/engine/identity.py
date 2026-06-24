"""Operator identity resolution for the response audit trail (FR-9).

The ``user`` field recorded on every ``--response`` entry (FR-2) is the *human
operator*, never the agent. ``config.identity()`` deliberately returns the
generic ``<name>@gauntlet.local`` commit identity, which would defeat the audit
trail, so it is never used here.

Resolution is deterministic and **fails closed** (CLAUDE.md §2): rather than
record an empty or placeholder ``user``, an unresolvable identity raises so the
FR-2 append never runs. The precedence and normalization are pinned exactly by
FR-9 so an exported-but-empty ``GAUNTLET_USER_EMAIL`` cannot shadow a valid git
config, and a missing/failing ``git config`` invocation cannot silently produce
a blank value.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping

#: Env var that overrides ``git config user.email`` for the audit field.
GAUNTLET_USER_EMAIL = "GAUNTLET_USER_EMAIL"

#: Verbatim FR-9 fail-closed message (asserted by tests; do not paraphrase).
_UNRESOLVED_MESSAGE = (
    "cannot resolve operator identity for the audit trail: set "
    "`GAUNTLET_USER_EMAIL` or `git config user.email`"
)


class OperatorIdentityError(RuntimeError):
    """Neither ``GAUNTLET_USER_EMAIL`` nor ``git config user.email`` yielded a
    non-empty operator identity (FR-9, fail closed). No response is appended."""


def _git_config_email(repo: Path) -> str | None:
    """``git config user.email`` for ``repo``, trimmed, or ``None``.

    Returns ``None`` — not a blank string — when git config is missing, the
    invocation exits non-zero, or the git binary is unavailable (``OSError``),
    so the caller cannot mistake a failed lookup for an empty-but-present value.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "config", "user.email"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def resolve_operator_identity(
    repo: Path, env: Mapping[str, str] | None = None
) -> str:
    """Resolve the human operator email for the FR-2 audit field (FR-9).

    Precedence: ``GAUNTLET_USER_EMAIL`` wins **only if** set and non-empty after
    trimming; otherwise ``git config user.email``. Whichever source is used is
    trimmed; a whitespace-only value is treated as unset (so an exported-but-empty
    env var does not shadow git config).

    Fails closed: if neither source yields a non-empty value, raises
    :class:`OperatorIdentityError` and records nothing. Validation is minimal —
    non-empty after trim only; RFC-5322 syntax is deferred (PRD §7), so a
    malformed-but-non-blank value is returned verbatim.
    """
    environ = os.environ if env is None else env
    raw = environ.get(GAUNTLET_USER_EMAIL)
    if raw is not None:
        trimmed = raw.strip()
        if trimmed:
            return trimmed
    git_email = _git_config_email(repo)
    if git_email:
        return git_email
    raise OperatorIdentityError(_UNRESOLVED_MESSAGE)
