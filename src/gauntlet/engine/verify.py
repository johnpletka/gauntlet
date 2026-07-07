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
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gauntlet.engine import gitops
from gauntlet.judge.hook_client import (
    DEFAULT_URL as _DEFAULT_JUDGE_URL,
    MODE_ENV_VAR,
    REPO_ROOT_ENV_VAR,
    RUN_ID_ENV_VAR,
    STEP_ID_ENV_VAR,
    URL_ENV_VAR,
)
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


# The EXACT run-local judge PreToolUse hook infrastructure keys the hook reads
# (gauntlet.judge.hook_client): judge url/token/mode + the run/step ids. Only
# these — never the caller's whole ``judge_env`` — are re-added onto the stripped
# verifier env. ``GAUNTLET_REPO_ROOT`` is re-added separately, re-pointed at the
# disposable copy. This closed set is what makes the strip-by-construction
# contract hold end-to-end (review F-003).
_JUDGE_HOOK_ENV_KEYS: frozenset[str] = frozenset(
    {URL_ENV_VAR, TOKEN_ENV_VAR, MODE_ENV_VAR, RUN_ID_ENV_VAR, STEP_ID_ENV_VAR}
)


def verifier_env(judge_env: dict[str, str], copy_root: Path) -> dict[str, str]:
    """The full environment the verifier (or the migrated enumeration) is spawned
    with: the stripped allowlist env PLUS only the run's judge *hook infrastructure*
    keys, with the judge's repo-root boundary re-pointed at the disposable copy
    (FR-2.5).

    ``GAUNTLET_REPO_ROOT`` → the copy means the PreToolUse judge hook denies any
    tool call whose resolved path escapes the copy (the read/write-denial
    mechanism). Only the exact hook keys the judge needs — its url/token/mode and
    the run/step ids (:data:`_JUDGE_HOOK_ENV_KEYS`) — are re-added on top of the
    stripped env; they are run-local infrastructure, not a credential the
    verifier's work should carry, but the hook requires them.

    Re-adding the *whole* ``judge_env`` would defeat the strip: a caller can pass
    environment-shaped judge data (the integration harness passes
    ``dict(os.environ)``) that carries ``ANTHROPIC_API_KEY`` / ``AWS_SECRET_ACCESS_KEY``
    / any ``*_TOKEN``/``*_KEY`` right back into the sandbox after the allowlist
    filter. Restricting the re-add to the hook-key allowlist keeps every
    secret-shaped var absent by construction — the run-local judge token is the
    only secret-shaped key that survives, and only because the hook cannot
    authenticate without it (review F-003)."""
    env = build_sandbox_env()
    source = judge_env or {}
    for key in _JUDGE_HOOK_ENV_KEYS:
        if key in source:
            env[key] = source[key]
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
    """Passive presence check for a candidate v1 backend; ``None`` when unavailable
    (FR-2.5, P5-A5).

    Requires the claude-code CLI on PATH **and** an active engine-managed judge
    for this run (a ``GAUNTLET_JUDGE_TOKEN`` in ``judge_env``) — without the judge
    the PreToolUse hook has nothing to call, so the read-denial contract cannot
    hold and the sub-step must park rather than run the verifier unhooked.

    This is presence only; :func:`probe_backend` additionally exercises the judge
    enforcement path before returning a backend (review F-001)."""
    claude = shutil.which("claude")
    if claude is None:
        return None
    if not (judge_env or {}).get(TOKEN_ENV_VAR):
        return None
    return SandboxBackend(claude_path=claude)


def _judge_roundtrip(url: str, token: str, body: dict) -> dict:
    """One authenticated ``/decide`` round-trip to the run's judge, isolated as a
    seam so the active hook probe (review F-001) is unit-testable without a live
    judge. Delegates to the same client the PreToolUse hook uses."""
    from gauntlet.judge import hook_client

    return hook_client._ask_judge(url, token, body)


def exercise_judge_hook(judge_env: dict[str, str] | None) -> None:
    """Actively confirm the run's judge enforcement backend — the service the
    PreToolUse hook calls on every tool call — is **live and authorizing this
    run**, before returning a backend (review F-001, P5-A5).

    Passive presence of a ``claude`` binary + a token string is not evidence that
    confinement is active: if the judge is down, foreign, or bound to a different
    run, the hook cannot enforce the copy-root read/write/network denial and the
    verifier would run effectively unconfined while Gauntlet believes it is jailed.
    So this issues one authenticated ``/decide`` round-trip (the exact client the
    hook uses) and requires the judge to answer this run with a well-formed
    permission decision. It fails closed — an unreachable judge, an HTTP/auth
    error, a run-id mismatch, or a malformed response raises
    :class:`SandboxUnavailableError` and parks the sub-step.

    Scope note (honest boundary): this confirms the *enforcement service* is live
    and bound to this run, so a dead/foreign judge parks closed rather than running
    a verifier unhooked. That claude actually *loads* the hook is the
    ``--setting-sources project`` + pinned-CLI invariant (:data:`_SETTING_SOURCES_FLAGS`,
    pins.yaml), exercised end-to-end by ``tests/integration/test_verifier_sandbox.py``;
    a decision value is deliberately NOT asserted here because the fast-path policy
    legitimately *allows* many in-copy tool calls, so requiring a specific verdict
    would false-park healthy runs."""
    source = judge_env or {}
    token = source.get(TOKEN_ENV_VAR)
    run_id = source.get(RUN_ID_ENV_VAR)
    if not token or not run_id:
        raise SandboxUnavailableError(
            "verifier hook probe: the run's judge token / run id are absent, so the "
            "judge PreToolUse enforcement path cannot be exercised — parking closed "
            "(the verifier never runs with unconfirmed enforcement; FR-2.5, P5-A5)."
        )
    url = source.get(URL_ENV_VAR) or _DEFAULT_JUDGE_URL
    body = {
        "tool_name": "Read",
        "tool_input": {"file_path": "gauntlet-verifier-hook-probe"},
        "repo_root": source.get(REPO_ROOT_ENV_VAR) or os.getcwd(),
        "run_id": run_id,
        "step_id": "verifier-hook-probe",
    }
    try:
        result = _judge_roundtrip(url, token, body)
    except Exception as exc:  # transport / HTTP / auth / decode — all fail closed
        raise SandboxUnavailableError(
            "verifier hook probe: the run's judge could not be reached to confirm "
            f"the PreToolUse enforcement path is live ({type(exc).__name__}: {exc}); "
            "parking closed — the verifier never runs with unconfirmed enforcement "
            "(FR-2.5, P5-A5)."
        ) from exc
    decision = (result or {}).get("decision")
    if decision not in ("allow", "deny", "ask"):
        raise SandboxUnavailableError(
            "verifier hook probe: the judge returned no well-formed decision "
            f"({decision!r}) for this run's canary call; the enforcement path is "
            "not confirmed, parking closed (FR-2.5, P5-A5)."
        )


def probe_backend(judge_env: dict[str, str] | None) -> SandboxBackend:
    """Return a usable, enforcement-confirmed backend or raise
    :class:`SandboxUnavailableError` (P5-A5).

    Two gates, both fail-closed: the passive presence check
    (:func:`detect_backend`) AND an active exercise of the run's judge enforcement
    path (:func:`exercise_judge_hook`) — so a claude binary + a stale token string
    is no longer enough to run the verifier while the judge is actually dead or
    bound to another run (review F-001)."""
    backend = detect_backend(judge_env)
    if backend is None:
        raise SandboxUnavailableError(
            "no usable v1 verifier backend: the claude-code CLI and an active "
            "engine-managed judge (whose PreToolUse hook enforces copy-root "
            "confinement) are required. Parking closed — the verifier never runs "
            "unhooked/unsandboxed (FR-2.5, P5-A5)."
        )
    exercise_judge_hook(judge_env)
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
# Engine-owned sentinels the backend wraps the collector's verbatim stdout in, so
# the enumeration output is recovered deterministically from the agent turn — a
# turn that omits/garbles the markers fails closed (parks), never a false "all
# mapped" pass. The markers are engine constants, never agent-authored.
_COLLECT_BEGIN = "<<<GAUNTLET-COLLECT-BEGIN>>>"
_COLLECT_END = "<<<GAUNTLET-COLLECT-END>>>"


def _capture_between_markers(text: str, begin: str, end: str) -> str | None:
    """Return the text strictly between the first ``begin`` and the next ``end``,
    or ``None`` if either marker is absent (fail-closed signal)."""
    if not text:
        return None
    i = text.find(begin)
    if i < 0:
        return None
    j = text.find(end, i + len(begin))
    if j < 0:
        return None
    return text[i + len(begin) : j]


def _run_backend_bash(
    backend: SandboxBackend,
    *,
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    allowed_tools: tuple[str, ...],
    timeout_s: float | None,
):
    """Launch the claude-code backend for one hook-confined Bash task in ``cwd``,
    returning its ``AgentResult``. Isolated as a seam so the collector-in-backend
    migration (review F-002) and its unit tests need no live CLI.

    The adapter is pinned to the verifier sandbox posture (``--setting-sources
    project`` so the judge hook fires, the copy-pointed stripped env), then narrowed
    to the requested tool allowlist — for enumeration, ``Bash`` only."""
    from gauntlet.adapters.claude_code import ClaudeCodeAdapter

    kwargs: dict = {"executable": backend.claude_path}
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    adapter = ClaudeCodeAdapter(**kwargs)
    configure_claude_verifier(adapter, env=env)
    adapter.allowed_tools = list(allowed_tools)
    return adapter.run(prompt, cwd=cwd)


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
    (review F-002 / P5-A7): in a **disposable copy** of the run worktree, executed
    through the claude-code judge-hooked ``Bash`` tool — NOT as a bare engine
    subprocess. ``pytest --collect-only`` imports the branch's ``conftest``/test
    modules, so it executes branch-authored code; routing it through the backend's
    Bash tool means every tool call is gated by the PreToolUse judge hook (network
    default-deny, copy-root path confinement) with the judge repo-root pointed at
    the copy, closing the gap where the interim bare subprocess ran the branch's
    import-time code outside the hook boundary.

    The collector's engine-owned command is handed to the backend to run verbatim;
    its stdout is recovered deterministically from between :data:`_COLLECT_BEGIN`/
    :data:`_COLLECT_END` markers and parsed by the collector's own parser.
    Fail-closed park-on-failure is preserved end to end: a backend launch/timeout
    failure, a turn that omits the markers, or an empty/unparseable enumeration
    raises :class:`~gauntlet.engine.collectors.CollectorEnumerationError`, and a
    copy-creation failure raises :class:`CopyCreationError` — both park the gate.
    A garbled agent turn therefore parks (recoverable), never passes the gate. The
    real worktree is untouched.

    ``mem_bytes`` is accepted for call-site compatibility but no longer applied: the
    OS ``RLIMIT`` of the interim subprocess posture is superseded by the backend's
    own wall-clock timeout and the judge hook's network/path denial."""
    from gauntlet.engine import collectors as _collectors

    copy = make_disposable_copy(worktree)
    command = " ".join(shlex.quote(str(c)) for c in collector.command)
    prompt = (
        "You are the collector-enumeration runner in a DISPOSABLE sandbox copy of a "
        "project. Using the Bash tool, run EXACTLY this one command and nothing "
        f"else:\n\n    {command}\n\n"
        "Then reply with ONLY the command's verbatim stdout, enclosed between these "
        "two markers each on their own line:\n"
        f"{_COLLECT_BEGIN}\n<the command's stdout, byte for byte>\n{_COLLECT_END}\n"
        "Do not summarize, reorder, de-duplicate, or add commentary."
    )
    try:
        result = _run_backend_bash(
            backend,
            prompt=prompt,
            cwd=copy.path,
            env=verifier_env(judge_env, copy.path),
            allowed_tools=("Bash",),
            timeout_s=timeout_s,
        )
    except _collectors.CollectorEnumerationError:
        raise
    except Exception as exc:  # any backend launch/run failure fails closed
        raise _collectors.CollectorEnumerationError(
            f"{collector.kind} enumeration could not run inside the sandbox backend: "
            f"{type(exc).__name__}: {exc} (fail closed — an absent enumeration is "
            "never treated as 'all mapped')"
        ) from exc
    finally:
        discard_disposable_copy(worktree, copy)

    captured = _capture_between_markers(result.text or "", _COLLECT_BEGIN, _COLLECT_END)
    if captured is None:
        raise _collectors.CollectorEnumerationError(
            f"{collector.kind} enumeration returned no marker-delimited output from "
            "the sandbox backend (fail closed — an unreadable enumeration is never "
            "treated as 'all mapped')"
        )
    ids = collector.parse(captured)
    if not ids:
        raise _collectors.CollectorEnumerationError(
            f"{collector.kind} enumeration produced no parseable ids inside the "
            "sandbox backend (fail closed — an unparseable enumeration is never "
            "treated as 'all mapped')"
        )
    return ids
