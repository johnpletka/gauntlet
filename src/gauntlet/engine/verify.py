"""Behavioral verifier + sandbox contract (pipeline-effectiveness FR-2.1/2.2/2.3/2.5, P5).

The verifier is an optional sub-step between review and triage: an agent on a
designated profile *executes* the phase deliverable in a **disposable copy** of
the post-handoff worktree and returns findings-schema output with
``category: behavioral`` and the executed commands as evidence — a signal class
no diff reader produces. Its findings join the merged panel and flow through the
same triage/fix/confirm machinery (FR-2.2, wired in ``cycle.py``); this module
owns the sandbox contract and the fail-closed infra handling.

**Sandbox backend (v1): claude-code + the engine's judge PreToolUse hook.**
Q3 was re-resolved (plan §7 amendment, 2026-07-06, human-ratified) to the
**claude-code** backend after the codex backend proved unrealizable on the
pinned CLI (codex ``exec`` fires no PreToolUse hooks — BOOTSTRAP-NOTES #10 — so
judge-enforced read-denial was fail-open, and ``codex sandbox`` needs an
unprovisioned ``[permissions]`` profile). claude-code launched with
``--setting-sources project`` fires the engine-managed judge hook on **every**
tool call, so the §7 read-denial contract is enforceable.

**Enforcement is hook-mediated (tool-call level), not OS-kernel-level** — the
deliberate, ratified v1 tradeoff. Confinement:

* **Read/write denial (outside the copy):** the verifier's judge session is
  pointed at the disposable copy as its repo-root boundary
  (``GAUNTLET_REPO_ROOT`` → copy), so the judge hook denies any Read/Write/Edit
  or ``Bash`` tool call whose resolved path (symlink-resolved, ``..``-normalized)
  escapes the copy — the same path-boundary machinery that gates the builder.
* **Network default-deny:** network-capable tools (WebFetch/WebSearch) are
  withheld from the verifier's ``allowed_tools`` and the judge hook denies
  network-reaching ``Bash``.
* **Env stripping:** the process is spawned from a rebuilt allowlist env
  (:func:`build_sandbox_env`) — every ``*_TOKEN``/``*_KEY``/``ANTHROPIC_*``/cloud
  cred absent by construction. This is the primary defense behind the honest
  **subprocess-boundary limit**: a ``Bash`` command's forked children are not
  independently hook-gated, but they inherit a credential-free env and a
  disposable, hash-guarded copy, so there is nothing to exfiltrate and no durable
  effect on the real tree. (claude authenticates from its own login dir under the
  allowlisted ``HOME``, not from a stripped ``*_KEY``.) Kernel-level subprocess
  isolation is the deferred post-v1 codex ``[permissions]`` backend.

Fail closed everywhere (FR-2.3): an unusable/unhooked backend, a copy-creation
failure, a sandbox-launch failure, or a wall-clock expiry **parks the cycle** —
the verifier never degrades to "skipped, proceed" and never runs unhooked.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gauntlet.engine import gitops
from gauntlet.judge.hook_client import REPO_ROOT_ENV_VAR
from gauntlet.judge.service import TOKEN_ENV_VAR


class VerifierError(Exception):
    """Base class for a fail-closed verifier fault — the cycle parks."""


class SandboxUnavailableError(VerifierError):
    """No usable, hook-confirmed sandbox backend at sub-step start (P5-A5).

    Raised by :func:`probe_backend` when the claude-code CLI is absent or the
    engine-managed judge hook cannot be confirmed active for this run. The
    verifier **never** falls back to running unhooked; the sub-step parks closed.
    """


class CopyCreationError(VerifierError):
    """Disposable-copy creation / sandbox launch failed (FR-2.3, P5-A2).

    Raised when the throwaway git worktree cannot be created. Parks the cycle —
    an absent copy is never "verify skipped, proceed"."""


class WorktreeMutatedError(VerifierError):
    """The real run worktree changed across verification (FR-2.5, P5-A4)."""


# --- environment stripping (FR-2.5 credential/secret env stripping) --------------
# Strip-by-construction: the verifier child is spawned from a *rebuilt* env holding
# only this explicit allowlist + the run's judge env (re-added by verifier_env so
# the PreToolUse hook can gate). Everything else — and specifically every
# secret/token/credential-shaped var — is never copied forward. claude
# authenticates from its own login dir under HOME (allowlisted), not from a
# provider *_KEY, so stripping those does not break the backend.
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM",
        "TMPDIR", "TEMP", "TMP", "SHELL", "USER", "LOGNAME", "PWD",
        # claude/runtime essentials: its login/config dir under HOME, SSL roots,
        # and node/uv locators so a real `uv run pytest`/build resolves toolchains.
        "CLAUDE_CONFIG_DIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_PATH", "NVM_DIR", "UV_CACHE_DIR",
    }
)

# Belt-and-braces secret shapes (FR-2.5). Nothing in ENV_ALLOWLIST matches these;
# the check documents intent and fails closed if the allowlist ever grows a
# secret-shaped name.
_SECRET_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_PASSWD",
                    "_CREDENTIAL", "_CREDENTIALS", "_SESSION", "_APIKEY")
_SECRET_PREFIXES = ("ANTHROPIC_", "OPENAI_", "AWS_", "GOOGLE_", "GCP_", "AZURE_",
                    "GEMINI_", "GH_", "GITHUB_", "GAUNTLET_JUDGE_", "GAUNTLET_REPO",
                    "CODEX_API")
_SECRET_EXACT = frozenset({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                           "AWS_SESSION_TOKEN"})


def is_secret_key(key: str) -> bool:
    """True iff ``key`` names a credential/secret-shaped env var (FR-2.5)."""
    upper = key.upper()
    return (
        upper in _SECRET_EXACT
        or upper.endswith(_SECRET_SUFFIXES)
        or upper.startswith(_SECRET_PREFIXES)
    )


def build_sandbox_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Rebuild the verifier child env from the allowlist only (FR-2.5).

    ``base`` defaults to the current process env. The result contains **only**
    allowlisted keys, and never a secret-shaped one — a defence-in-depth filter on
    top of the allowlist. ``PYTHONDONTWRITEBYTECODE`` keeps an in-copy pytest run
    filesystem-side-effect-lean. The run's judge env is re-added separately by
    :func:`verifier_env` (the hook needs it), so it is intentionally NOT here."""
    src = os.environ if base is None else base
    env = {
        k: src[k]
        for k in ENV_ALLOWLIST
        if k in src and not is_secret_key(k)
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def verifier_env(judge_env: dict[str, str], copy_root: Path) -> dict[str, str]:
    """The full environment the verifier (or the migrated enumeration) is spawned
    with: the stripped allowlist env PLUS the run's judge env, with the judge's
    repo-root boundary re-pointed at the disposable copy (FR-2.5).

    ``GAUNTLET_REPO_ROOT`` → the copy means the PreToolUse judge hook denies any
    tool call whose resolved path escapes the copy (the read/write-denial
    mechanism). The judge token/url/mode are re-added on top of the stripped env
    so the hook can actually call the judge — they are run-local infrastructure,
    not a credential the verifier's work should carry, but the hook requires them."""
    env = build_sandbox_env()
    env.update(judge_env or {})
    env[REPO_ROOT_ENV_VAR] = str(copy_root)
    return env


# --- sandbox backend (claude-code + judge hook) ----------------------------------
# Tools the verifier may use: read + run + edit inside the copy. No network tools
# (WebFetch/WebSearch) — network default-deny (FR-2.5). Edit/Write are allowed
# because the verifier *executes* the deliverable (may build/patch inside the
# throwaway copy); the judge hook confines every path to the copy root.
VERIFIER_ALLOWED_TOOLS: tuple[str, ...] = ("Bash", "Read", "Grep", "Glob", "Edit", "Write")
VERIFIER_PERMISSION_MODE = "acceptEdits"
# --setting-sources project makes claude load the repo's PreToolUse judge hook
# (pins.yaml: claude only fires the engine-managed hook under this setting).
_SETTING_SOURCES_FLAGS = ("--setting-sources", "project")


@dataclass(frozen=True)
class SandboxBackend:
    """A resolved v1 verifier backend: the claude-code executable, hook-confirmed.

    There is no OS-jail wrap here (that was the abandoned codex surface); the jail
    is the claude-code PreToolUse judge hook, applied per tool call and pointed at
    the disposable copy by :func:`verifier_env`."""

    claude_path: str


def detect_backend(judge_env: dict[str, str] | None) -> SandboxBackend | None:
    """Probe for a usable v1 backend; ``None`` when unavailable (FR-2.5, P5-A5).

    Requires the claude-code CLI on PATH **and** an active engine-managed judge
    for this run (a ``GAUNTLET_JUDGE_TOKEN`` in ``judge_env``) — without the judge
    the PreToolUse hook has nothing to call, so the read-denial contract cannot
    hold and the sub-step must park rather than run the verifier unhooked."""
    claude = shutil.which("claude")
    if claude is None:
        return None
    if not (judge_env or {}).get(TOKEN_ENV_VAR):
        return None
    return SandboxBackend(claude_path=claude)


def probe_backend(judge_env: dict[str, str] | None) -> SandboxBackend:
    """Return a usable backend or raise :class:`SandboxUnavailableError` (P5-A5)."""
    backend = detect_backend(judge_env)
    if backend is None:
        raise SandboxUnavailableError(
            "no usable v1 verifier backend: the claude-code CLI and an active "
            "engine-managed judge (whose PreToolUse hook enforces copy-root "
            "confinement) are required. Parking closed — the verifier never runs "
            "unhooked/unsandboxed (FR-2.5, P5-A5)."
        )
    return backend


def configure_claude_verifier(adapter, *, env: dict[str, str]) -> list[str]:
    """Pin an already-built claude-code adapter to the verifier sandbox posture
    (FR-2.5): the confined ``allowed_tools`` allowlist (no network tools), the
    ``acceptEdits`` permission mode, the ``--setting-sources project`` flag so the
    judge hook fires, and the rebuilt copy-pointed env. Returns no extra flags.

    A no-op on a non-claude adapter (a test double) — it only touches attributes a
    claude-code adapter exposes, so the caller's fail-closed profile validation
    stays the authority on whether the profile is actually claude-code."""
    if hasattr(adapter, "allowed_tools"):
        adapter.allowed_tools = list(VERIFIER_ALLOWED_TOOLS)
    if hasattr(adapter, "permission_mode"):
        adapter.permission_mode = VERIFIER_PERMISSION_MODE
    if hasattr(adapter, "base_flags"):
        flags = list(getattr(adapter, "base_flags") or [])
        if "--setting-sources" not in flags:
            flags += list(_SETTING_SOURCES_FLAGS)
        adapter.base_flags = flags
    if hasattr(adapter, "env"):
        adapter.env = dict(env)
    return []


# --- disposable copy (FR-2.1 / FR-2.3) -------------------------------------------
@dataclass
class DisposableCopy:
    """A throwaway git worktree of the post-handoff tree the verifier executes in.

    ``path`` is the workspace root handed to the sandbox; ``root`` is the temp
    parent that :func:`discard_disposable_copy` unlinks. The real run worktree is
    never this directory."""

    path: Path
    root: Path


def make_disposable_copy(repo_root: Path, *, parent_dir: Path | None = None) -> DisposableCopy:
    """Create a disposable git worktree of ``repo_root`` at HEAD (FR-2.1).

    Uses ``git worktree add --detach`` so the copy is a faithful checkout of the
    committed post-handoff tree without contending for the run branch. Fails
    closed (:class:`CopyCreationError`) on any git error — an absent copy parks the
    sub-step, never "verify skipped, proceed" (FR-2.3, P5-A2)."""
    try:
        root = Path(tempfile.mkdtemp(prefix="gauntlet-verify-", dir=parent_dir))
    except OSError as exc:
        raise CopyCreationError(f"verifier: could not create a temp root: {exc}") from exc
    copy = root / "worktree"
    try:
        gitops.add_worktree(repo_root, copy, "HEAD")
    except gitops.GitError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise CopyCreationError(
            f"verifier: disposable worktree copy could not be created: {exc} "
            "(fail closed — the cycle parks, never proceeds without a copy)"
        ) from exc
    return DisposableCopy(path=copy, root=root)


def discard_disposable_copy(repo_root: Path, copy: DisposableCopy) -> None:
    """Tear down a disposable copy (best-effort; never raises)."""
    try:
        gitops.remove_worktree(repo_root, copy.path)
    except gitops.GitError:
        pass
    shutil.rmtree(copy.root, ignore_errors=True)
    try:
        gitops.prune_worktrees(repo_root)
    except gitops.GitError:
        pass


# --- collector-enumeration migration into the backend (P5-A7) --------------------
def enumerate_in_sandbox(
    backend: SandboxBackend,
    collector,
    *,
    worktree: Path,
    judge_env: dict[str, str],
    timeout_s: float | None = None,
    mem_bytes: int | None = None,
) -> set[str]:
    """Run a collector's side-effect-free enumeration INSIDE the v1 sandbox backend
    (review F-002 / P5-A7): in a **disposable copy** of the run worktree, so the
    branch-authored ``conftest``/test code that ``pytest --collect-only`` imports
    executes read-confined to the copy (never the real worktree) with the run's
    judge env pointed at the copy root and a resource/wall-clock bound.

    This is the migration off the P2-P4 interim posture (which ran enumeration as a
    bare subprocess in the *real* worktree). Fail-closed park-on-failure is
    preserved: a failed/timed-out/unparseable enumeration raises
    :class:`~gauntlet.engine.collectors.CollectorEnumerationError`, and a
    copy-creation failure raises :class:`CopyCreationError` — both park the gate.
    The real worktree is untouched."""
    from gauntlet.engine import collectors as _collectors

    copy = make_disposable_copy(worktree)
    kwargs: dict = {"worktree": copy.path, "judge_env": verifier_env(judge_env, copy.path)}
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    if mem_bytes is not None:
        kwargs["mem_bytes"] = mem_bytes
    try:
        return collector.enumerate(**kwargs)
    finally:
        discard_disposable_copy(worktree, copy)
