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

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gauntlet.engine import gitops
from gauntlet.judge.hook_client import (
    DEFAULT_URL as _DEFAULT_JUDGE_URL,
    MODE_ENV_VAR,
    PROBE_STEP_PREFIX,
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

# The claude CLI's own OAuth token. On hosts whose claude credential is
# Keychain-backed (macOS; no `~/.claude/.credentials.json`), the scratch-HOME
# repoint (F-006) breaks the CLI's Keychain lookup, so the verifier canary comes
# up "Not logged in" and the hook-loading probe false-parks the whole impl-cycle
# (#67). The token is re-added onto the stripped verifier env — the same conceded
# residual as the run judge token (":func:`_JUDGE_HOOK_ENV_KEYS`"): the one
# credential the CLI itself must hold to run at all, NOT a provider `*_KEY` the
# verifier's *work* should carry. It is re-added AFTER :func:`build_sandbox_env`
# (which strips it as `*_TOKEN`-shaped), exactly like the judge token, so
# strip-by-construction still holds for every other secret.
CLAUDE_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# The macOS login-Keychain service the claude CLI stores its OAuth credential
# under; the value is a JSON blob whose `claudeAiOauth.accessToken` is what the
# CLI authenticates with.
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
# Wall-clock cap on the `security` read so a GUI Keychain-unlock prompt (which
# would block a non-interactive engine) fails to None instead of hanging.
_KEYCHAIN_READ_TIMEOUT_S = 10.0


def _extract_keychain_oauth_token() -> str | None:
    """Best-effort read of the claude OAuth access token from the macOS login
    Keychain (#67, hybrid path). Returns ``None`` on ANY failure — non-darwin, no
    ``security`` tool, a non-zero/ empty read, a GUI-prompt timeout, or a blob we
    cannot parse — so the caller falls back to the fail-closed park rather than
    guessing. Never raises; never logs the token.

    The extracted access token is short-lived (the CLI normally refreshes it via
    the Keychain's refresh token, which an injected env snapshot cannot do): if it
    has expired, the canary re-parks with the same clear "Not logged in" message,
    and the operator can set a long-lived ``CLAUDE_CODE_OAUTH_TOKEN``
    (`claude setup-token`) instead."""
    if sys.platform != "darwin":
        return None
    security = shutil.which("security")
    if security is None:
        return None
    try:
        proc = subprocess.run(
            [security, "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=_KEYCHAIN_READ_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    blob = (proc.stdout or "").strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return None
    tok = (data.get("claudeAiOauth") or {}).get("accessToken") if isinstance(data, dict) else None
    return tok if isinstance(tok, str) and tok.strip() else None


def resolve_claude_oauth_token(source: dict[str, str] | None = None) -> str | None:
    """The claude OAuth token to hand the sandboxed verifier, or ``None`` (#67).

    Resolution order, so an existing login "just works" while an explicit token
    remains an override:

    1. An explicit ``CLAUDE_CODE_OAUTH_TOKEN`` in ``source`` (the run's
       ``judge_env``), then the parent process env — a long-lived
       ``claude setup-token`` an operator exported; robust across expiry/CI.
    2. Otherwise (macOS), the operator's **existing** login session, read from the
       login Keychain (:func:`_extract_keychain_oauth_token`) — no setup needed.

    Empty/whitespace is treated as absent (fail-closed: an empty token would only
    re-trigger the "Not logged in" park). ``None`` when nothing resolves, leaving
    the sub-step to park closed with its actionable message."""
    for env in (source or {}, os.environ):
        tok = env.get(CLAUDE_OAUTH_TOKEN_ENV_VAR)
        if tok and tok.strip():
            return tok
    return _extract_keychain_oauth_token()


def verifier_env(
    judge_env: dict[str, str],
    copy_root: Path,
    *,
    step_id: str | None = None,
    scratch_home: Path | None = None,
) -> dict[str, str]:
    """The full environment the verifier (or the migrated enumeration) is spawned
    with: the stripped allowlist env PLUS only the run's judge *hook infrastructure*
    keys, with the judge's repo-root boundary re-pointed at the disposable copy
    (FR-2.5).

    The confinement mechanism is the judge-side per-step BOUNDARY the engine
    registers before launch (:func:`register_boundary`, PR #59 review B1) —
    server-authoritative, keyed on the verifier's own ``step_id`` set here.
    ``GAUNTLET_REPO_ROOT`` → the copy remains as the request-side fallback for a
    standalone (un-pinned) judge; on the production pinned-root judge it is
    advisory only, which is exactly why the registered boundary exists. Only the
    exact hook keys the judge needs — its url/token/mode and the run/step ids
    (:data:`_JUDGE_HOOK_ENV_KEYS`) — are re-added on top of the stripped env;
    they are run-local infrastructure, not a credential the verifier's work
    should carry, but the hook requires them.

    Re-adding the *whole* ``judge_env`` would defeat the strip: a caller can pass
    environment-shaped judge data (the integration harness passes
    ``dict(os.environ)``) that carries ``ANTHROPIC_API_KEY`` / ``AWS_SECRET_ACCESS_KEY``
    / any ``*_TOKEN``/``*_KEY`` right back into the sandbox after the allowlist
    filter. Restricting the re-add to the hook-key allowlist keeps every
    secret-shaped var absent by construction — the run-local judge token is the
    only secret-shaped key that survives, and only because the hook cannot
    authenticate without it (review F-003).

    One further secret-shaped key survives the strip when the host provides it:
    ``CLAUDE_CODE_OAUTH_TOKEN`` (:func:`resolve_claude_oauth_token`). It is the
    claude CLI's own headless-auth token — the credential the backend must hold to
    run at all — re-added so the canary authenticates under the scratch HOME
    instead of the Keychain the repoint hides (#67). Same class as the judge
    token, not a provider ``*_KEY``."""
    env = build_sandbox_env()
    source = judge_env or {}
    for key in _JUDGE_HOOK_ENV_KEYS:
        if key in source:
            env[key] = source[key]
    env[REPO_ROOT_ENV_VAR] = str(copy_root)
    # Re-add the claude CLI's own OAuth token if the host provides one (#67), so a
    # Keychain-auth host authenticates the canary under the scratch HOME. Added
    # after build_sandbox_env (which strips it as `*_TOKEN`), exactly like the
    # judge token — every other secret stays absent by construction.
    oauth_token = resolve_claude_oauth_token(source)
    if oauth_token is not None:
        env[CLAUDE_OAUTH_TOKEN_ENV_VAR] = oauth_token
    # The verifier's OWN step id (PR #59 review B1/F-003) — never inherited from
    # `judge_env` (the cycle step's id would confine the fixer too). It is (a)
    # what the judge's per-step boundary registration keys on, and (b) a
    # non-empty step id, so `pipeline_step_only` denies (in-run push/PR, FR-9.8)
    # apply to the verifier like every in-run agent.
    if step_id is not None:
        env[STEP_ID_ENV_VAR] = step_id
    # Scratch HOME (PR #59 review F-006): hook-mediated confinement cannot gate
    # forked subprocesses, and the previous allowlisted real HOME let an
    # un-hooked child read ~/.aws, ~/.ssh, ~/.config/gh directly. Pointing HOME
    # at an empty scratch dir removes that discovery surface; the claude CLI
    # still finds its login via CLAUDE_CONFIG_DIR, pinned to the real config
    # dir (which remains the one credential the CLI itself must hold).
    if scratch_home is not None:
        real_home = env.get("HOME") or os.environ.get("HOME") or ""
        if "CLAUDE_CONFIG_DIR" not in env and real_home:
            env["CLAUDE_CONFIG_DIR"] = str(Path(real_home) / ".claude")
        # Offline toolchain provisioning (#69): the disposable copy has no `.venv`
        # (gitignored) and the scratch HOME hides `~/.cache/uv`, while the sandbox
        # denies network — so `uv run pytest` cannot build the test env and the
        # verifier hangs to the turn timeout. Point uv at the REAL cache and force
        # offline so it provisions a fresh venv INSIDE the copy from cache only (no
        # network — the deny stands; a cache miss fails fast instead of hanging).
        # The venv lives in the throwaway copy; the real one is never touched.
        uv_cache = os.environ.get("UV_CACHE_DIR") or (
            str(Path(real_home) / ".cache" / "uv") if real_home else ""
        )
        if uv_cache:
            env["UV_CACHE_DIR"] = uv_cache
        env["UV_OFFLINE"] = "1"
        env["HOME"] = str(scratch_home)
    return env


# --- sandbox backend (claude-code + judge hook) ----------------------------------
# Tools the verifier may use: read + run + edit inside the copy. No network tools
# (WebFetch/WebSearch) — network default-deny (FR-2.5). Edit/Write are allowed
# because the verifier *executes* the deliverable (may build/patch inside the
# throwaway copy); the judge hook confines every path to the copy root.
VERIFIER_ALLOWED_TOOLS: tuple[str, ...] = ("Bash", "Read", "Grep", "Glob", "Edit", "Write")
VERIFIER_PERMISSION_MODE = "acceptEdits"
# §7 item 5 resource caps (PR #59): generous — the point is bounding a runaway
# verifier turn (fork bombs, memory blowups), not failing a healthy one.
VERIFIER_MEM_BYTES = 8 * 1024**3
VERIFIER_CPU_SECONDS = 3600
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
    enforcement path AND proves a real claude turn loads/fires the hook before
    returning a backend (review F-001)."""
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


def _judge_observed(url: str, token: str, run_id: str, nonce: str) -> bool:
    """Ask the run's judge whether it OBSERVED a PreToolUse callback tagged with
    ``nonce`` (review F-001). Isolated as a seam so :func:`confirm_hook_loaded` is
    unit-testable without a live judge or claude CLI."""
    from gauntlet.judge import hook_client

    result = hook_client._ask_observed(url, token, run_id, nonce)
    return bool((result or {}).get("observed"))


def _judge_boundary(url: str, token: str, body: dict, *, clear: bool = False) -> dict:
    """One authenticated ``/boundary`` (or ``/boundary/clear``) round-trip,
    isolated as a seam so the lease functions are unit-testable without a live
    judge."""
    from gauntlet.judge import hook_client

    return hook_client._ask_boundary(url, token, body, clear=clear)


@dataclass(frozen=True)
class BoundaryLease:
    """An engine-held lease on a judge-side per-step boundary (PR #59 B1).

    ``key`` is the clear secret minted at registration; it never enters the
    sandbox env, so the sandboxed agent (which necessarily holds the run token
    for its hook) cannot clear or re-register its own boundary."""

    step_id: str
    key: str
    url: str
    token: str
    run_id: str | None


def make_scratch_home(root: Path) -> Path:
    """A minimal scratch HOME for the sandboxed claude child (PR #59 F-006).

    Re-pointing HOME removes the discovery surface un-hooked subprocess
    children previously had (``~/.aws``, ``~/.ssh``, ``~/.config/gh`` were all
    readable through the allowlisted real HOME). The claude CLI itself still
    needs its login: the scratch home is seeded with symlinks to ONLY that
    surface — ``~/.claude`` (config/credentials; also pinned via
    ``CLAUDE_CONFIG_DIR``) and, on darwin, ``~/Library/Keychains`` (keychain-
    backed OAuth). This is the conceded §7 item-6 residual (the CLI's own
    credential), and nothing else. If login still fails, the hook-loading
    probe parks the sub-step closed before any verifier work runs."""
    scratch = root / "home"
    scratch.mkdir(parents=True, exist_ok=True)
    real = Path(os.environ.get("HOME", "")).expanduser()
    if str(real) != "." and real.exists():
        seeds = [".claude"]
        if sys.platform == "darwin":
            seeds.append("Library/Keychains")
        for rel in seeds:
            src = real / rel
            dst = scratch / rel
            if src.exists() and not (dst.exists() or dst.is_symlink()):
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(src)
    return scratch


def register_boundary(
    judge_env: dict[str, str] | None, step_id: str, root: Path
) -> BoundaryLease:
    """Bind ``step_id`` to the disposable-copy ``root`` on the run's judge,
    BEFORE the sandboxed turn launches (PR #59 review B1).

    This is what makes the copy confinement real on the production pinned-root
    judge: requests carrying ``step_id`` are confined to ``root`` (reads,
    writes, Bash paths, network default-deny, git refs) and the boundary
    replaces the pinned root as the effective path boundary. Fails closed —
    any transport/HTTP fault or a refused (already-bound) registration raises
    :class:`SandboxUnavailableError` and parks the sub-step."""
    source = judge_env or {}
    token = source.get(TOKEN_ENV_VAR)
    if not token:
        raise SandboxUnavailableError(
            "verifier boundary: no judge token in the run env — the per-step "
            "boundary cannot be registered, parking closed (PR #59 B1)."
        )
    url = source.get(URL_ENV_VAR) or _DEFAULT_JUDGE_URL
    run_id = source.get(RUN_ID_ENV_VAR)
    key = secrets.token_hex(16)
    body = {"step_id": step_id, "root": str(root), "key": key, "run_id": run_id}
    try:
        result = _judge_boundary(url, token, body)
    except Exception as exc:  # transport / HTTP / auth / decode — fail closed
        raise SandboxUnavailableError(
            "verifier boundary: the judge refused or could not record the "
            f"disposable-copy boundary for step {step_id!r} "
            f"({type(exc).__name__}: {exc}); parking closed — the verifier "
            "never runs without a registered boundary (PR #59 B1)."
        ) from exc
    if not (result or {}).get("registered"):
        raise SandboxUnavailableError(
            f"verifier boundary: the judge did not confirm registration for "
            f"step {step_id!r} ({result!r}); parking closed (PR #59 B1)."
        )
    return BoundaryLease(step_id=step_id, key=key, url=url, token=token, run_id=run_id)


def clear_boundary(lease: BoundaryLease) -> None:
    """Release a boundary lease (best-effort; never raises — a stale entry in
    the judge's in-memory registry is inert once its unique step_id retires)."""
    body = {"step_id": lease.step_id, "key": lease.key, "run_id": lease.run_id}
    try:
        _judge_boundary(lease.url, lease.token, body, clear=True)
    except Exception:
        pass


def confirm_boundary_enforced(lease: BoundaryLease, outside_path: Path) -> None:
    """Prove, on the LIVE judge, that the registered boundary denies an
    outside-copy read before the sandboxed turn runs (PR #59 review B1/F-002).

    Issues one synthetic ``/decide`` carrying the verifier's step id and a path
    outside the copy (the run worktree), with the request-side ``repo_root``
    deliberately set to that path's own parent — so a judge that ignored the
    registered boundary would see an in-root read and NOT hit the confinement
    rung. Only the deterministic confinement deny
    (``matched_rule: verifier-boundary-path``) passes; anything else — allow,
    ask, a classifier verdict, a transport fault — parks closed."""
    body = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(outside_path)},
        "repo_root": str(outside_path.parent),
        "run_id": lease.run_id,
        "step_id": lease.step_id,
    }
    try:
        result = _judge_roundtrip(lease.url, lease.token, body)
    except Exception as exc:
        raise SandboxUnavailableError(
            "verifier boundary probe: the judge could not be queried to prove "
            f"outside-copy denial ({type(exc).__name__}: {exc}); parking closed "
            "(PR #59 B1, FR-2.5)."
        ) from exc
    decision = (result or {}).get("decision")
    rule = (result or {}).get("matched_rule")
    if decision != "deny" or rule != "verifier-boundary-path":
        raise SandboxUnavailableError(
            "verifier boundary probe: an outside-copy read was NOT denied by the "
            f"registered boundary (decision={decision!r}, rule={rule!r}) — the "
            "§7 read-confinement contract is not active for this step; parking "
            "closed (PR #59 B1, FR-2.5)."
        )


# Wall-clock ceiling on the hook-loading probe's claude turn (review F-001). It
# does a single trivial in-copy Bash call, so a healthy backend finishes in
# seconds; an over-limit turn is killed and parks closed (never runs unhooked).
_PROBE_TIMEOUT_S = 120.0


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

    Scope note: this is the CHEAP liveness/binding pre-check — it confirms the
    *enforcement service* is live and bound to this run, so a dead/foreign judge
    parks closed fast, before the more expensive step. That claude actually *loads
    and fires* the hook under ``--setting-sources project`` is no longer merely an
    invariant deferred to integration tests: :func:`confirm_hook_loaded` proves it
    at runtime by launching a real claude turn and requiring the judge to observe
    its PreToolUse callback (review F-001). A decision value is deliberately NOT
    asserted here because the fast-path policy legitimately *allows* many in-copy
    tool calls, so requiring a specific verdict would false-park healthy runs."""
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


def confirm_hook_loaded(
    backend: SandboxBackend,
    judge_env: dict[str, str] | None,
    *,
    repo_root: Path,
    parent_dir: Path | None = None,
) -> None:
    """Prove END-TO-END that a real claude-code turn LOADS and FIRES the engine's
    PreToolUse judge hook — the strong gate the ``/decide`` liveness pre-check
    (:func:`exercise_judge_hook`) cannot give (review F-001, P5-A5).

    A live judge answering a synthetic ``/decide`` is NOT evidence that *claude*
    invokes the hook under ``--setting-sources project``: if claude were pinned or
    configured such that the hook never loads, the judge would answer healthy while
    the verifier ran effectively unconfined. So this launches a minimal real
    claude-code turn — configured by :func:`configure_claude_verifier`, in a
    **disposable copy**, under ``GAUNTLET_STEP_ID = PROBE_STEP_PREFIX + <nonce>`` —
    and instructs it to make ONE harmless in-copy tool call. If claude loads the
    hook, that tool call reaches the judge tagged with the nonce; the judge records
    it, and this then queries ``/observed`` and requires the nonce to be present
    before returning.

    Fail closed on everything (FR-2.3/2.5): a copy-creation failure, a
    launch/configuration fault, a wall-clock expiry, an ``/observed`` query error,
    or a turn the judge never observed for this nonce all raise
    :class:`SandboxUnavailableError` and park the sub-step. The disposable copy is
    always discarded; the real worktree is untouched."""
    source = judge_env or {}
    token = source.get(TOKEN_ENV_VAR)
    run_id = source.get(RUN_ID_ENV_VAR)
    if not token or not run_id:
        raise SandboxUnavailableError(
            "verifier hook-loading probe: the run's judge token / run id are "
            "absent, so a hook-firing claude turn cannot be bound to this run — "
            "parking closed (FR-2.5, P5-A5)."
        )
    url = source.get(URL_ENV_VAR) or _DEFAULT_JUDGE_URL
    nonce = secrets.token_hex(16)
    prompt = (
        "You are a sandbox self-test in a DISPOSABLE copy of a project. Using the "
        "Bash tool, run EXACTLY this one command and nothing else:\n\n"
        f"    echo {nonce}\n\n"
        "Then reply with a single word: done. Do not run any other command."
    )
    try:
        copy = make_disposable_copy(repo_root, parent_dir=parent_dir)
    except CopyCreationError as exc:
        raise SandboxUnavailableError(
            "verifier hook-loading probe: could not create a disposable copy to "
            f"exercise the PreToolUse hook ({exc}); parking closed (FR-2.3, P5-A5)."
        ) from exc
    # The probe env is the exact verifier posture — probe step_id (so every tool
    # call reaches the judge carrying the nonce), scratch HOME, and a registered
    # judge-side boundary on the probe's own step id (so the probe turn runs
    # under the same confinement discipline the real turn will, PR #59 B1).
    probe_step_id = f"{PROBE_STEP_PREFIX}{nonce}"
    scratch_home = make_scratch_home(copy.root)
    env = verifier_env(
        judge_env or {}, copy.path, step_id=probe_step_id, scratch_home=scratch_home
    )
    lease = register_boundary(judge_env, probe_step_id, copy.path)
    try:
        _run_backend_bash(
            backend,
            prompt=prompt,
            cwd=copy.path,
            env=env,
            allowed_tools=("Bash",),
            timeout_s=_PROBE_TIMEOUT_S,
        )
    except Exception as exc:  # launch / config / timeout — all fail closed
        raise SandboxUnavailableError(
            "verifier hook-loading probe: the canary claude turn could not be "
            f"launched to exercise the PreToolUse hook ({type(exc).__name__}: "
            f"{exc}); parking closed — the verifier never runs unhooked (FR-2.5, "
            "P5-A5)."
        ) from exc
    finally:
        clear_boundary(lease)
        discard_disposable_copy(repo_root, copy)

    try:
        seen = _judge_observed(url, token, run_id, nonce)
    except Exception as exc:  # transport / HTTP / decode — fail closed
        raise SandboxUnavailableError(
            "verifier hook-loading probe: the judge could not be queried to "
            f"confirm it observed the hook callback ({type(exc).__name__}: {exc}); "
            "parking closed (FR-2.5, P5-A5)."
        ) from exc
    if not seen:
        raise SandboxUnavailableError(
            "verifier hook-loading probe: the canary claude turn ran but the judge "
            "never observed a PreToolUse callback for it — the hook did not load/"
            "fire under --setting-sources project, so the read/write/network denial "
            "contract is NOT active. Parking closed — the verifier never runs "
            "unhooked (FR-2.5, P5-A5)."
        )


def probe_backend(
    judge_env: dict[str, str] | None,
    *,
    repo_root: Path,
    parent_dir: Path | None = None,
) -> SandboxBackend:
    """Return a usable, enforcement-confirmed backend or raise
    :class:`SandboxUnavailableError` (P5-A5).

    Three gates, all fail-closed: the passive presence check
    (:func:`detect_backend`), a cheap active exercise of the run's judge
    enforcement path (:func:`exercise_judge_hook`), AND an end-to-end proof that a
    real claude-code turn actually LOADS and FIRES the PreToolUse hook
    (:func:`confirm_hook_loaded`) — so neither a claude binary + a stale token
    string, nor a live judge that claude never actually calls, is enough to run the
    verifier while confinement is not truly active (review F-001)."""
    backend = detect_backend(judge_env)
    if backend is None:
        raise SandboxUnavailableError(
            "no usable v1 verifier backend: the claude-code CLI and an active "
            "engine-managed judge (whose PreToolUse hook enforces copy-root "
            "confinement) are required. Parking closed — the verifier never runs "
            "unhooked/unsandboxed (FR-2.5, P5-A5)."
        )
    exercise_judge_hook(judge_env)
    confirm_hook_loaded(backend, judge_env, repo_root=repo_root, parent_dir=parent_dir)
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
    # §7 item 5 (PR #59): best-effort memory/CPU caps on the verifier process
    # group, alongside the wall-clock timeout. rlimits are inherited by forked
    # children, so this is one of the few §7 controls that survives the
    # subprocess boundary the hook cannot cross. Best-effort by platform
    # (macOS ignores RLIMIT_AS); the wall-clock kill remains the hard ceiling.
    if hasattr(adapter, "spawn_preexec") and os.name == "posix":
        from gauntlet.engine.collectors import _rlimit_preexec

        adapter.spawn_preexec = _rlimit_preexec(
            VERIFIER_MEM_BYTES, VERIFIER_CPU_SECONDS
        )
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


# The collector-enumeration path is an ENGINE SUBPROCESS in a disposable copy
# (collectors.run_bounded_enumeration, PR #59 review F3/F7) — the P5 design
# that routed `pytest --collect-only` stdout through a claude turn asked to
# echo it byte-for-byte is retired: an LLM in the evidence path could both
# truncate a large id list (chronic false park) and fabricate ids (false
# pass), defeating the deterministic-gate premise of FR-3.2.


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


