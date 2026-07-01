"""``gauntlet doctor`` — environment validation (FR-1.3, FR-1.5).

Validates that a clone is ready to run a pipeline: the agent CLIs are installed
(and their versions match what the pin file verified), the judge is startable,
the hook wiring is in place, and an ApiAdapter credential is present in the
environment (never in repo config — FR-1.4). Each check yields an actionable
:class:`CheckResult`; the CLI prints them and exits non-zero if any FAILs.

The checks take their environment through an injectable :class:`DoctorProbes`
so the unit suite can simulate broken environments (missing CLI, stale version,
absent hooks, missing key) without touching the real machine — the live CLI
probes only run when the real default probes are used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gauntlet import __version__
from gauntlet.engine.init import HOOK_COMMAND
from gauntlet.pins import PinFile, load_pins, pin_file_path

OK = "ok"
WARN = "warn"
FAIL = "fail"

# The two agent CLIs doctor checks for and pins.
AGENT_CLIS = ("claude", "codex")

# model-name prefix -> the env var that must carry its key (FR-1.4). Matched
# case-insensitively against the start of the configured model name.
_KEY_BY_PREFIX = {
    "gpt": "OPENAI_API_KEY",
    "o1": "OPENAI_API_KEY",
    "o3": "OPENAI_API_KEY",
    "o4": "OPENAI_API_KEY",
    "openai/": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "anthropic/": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google/": "GEMINI_API_KEY",
}

# Credential literals that must never appear in committed repo config (FR-1.4).
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),       # OpenAI / Anthropic style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),            # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),      # Google API key
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str          # OK | WARN | FAIL
    detail: str
    remedy: str | None = None


def _no_auth_probe(_name: str) -> bool | None:
    """Default auth probe: 'could not determine' (used by offline unit tests)."""
    return None


def _real_tracker_auth_probe(config, env: Mapping[str, str]) -> str | None:
    """None if the configured tracker authenticates, else a short error (FR-10.1).

    Builds the provider via the entry-point registry, resolves the token by
    env-var name, and runs the cheap ``verify_auth`` probe under the configured
    per-call timeout (FR-6.4). Injected so the tracker check runs offline in the
    unit suite."""
    from gauntlet.trackers import get_tracker
    from gauntlet.trackers.base import IssueTrackerError

    try:
        tracker = get_tracker(config, env=env)
        tracker.verify_auth()
    except IssueTrackerError as exc:
        return str(exc)
    return None


def _real_judge_model_resolvable(model: str) -> str | None:
    """None if LiteLLM can resolve a provider for ``model``, else a short error.
    Delegates to the shared :func:`model_provider_error` so doctor and the judge
    startup path agree on what "resolvable" means (PR #13 review)."""
    from gauntlet.adapters.api import model_provider_error

    return model_provider_error(model)


@dataclass(frozen=True)
class DoctorProbes:
    """Injectable environment access so checks are testable offline."""

    # name -> version string (e.g. "2.1.172"), or None if the CLI is absent.
    cli_version: Callable[[str], str | None]
    env: Mapping[str, str]
    # name -> True (authenticated) / False (logged out or broken) / None
    # (could not determine). FR-1.3: a logged-out CLI must FAIL doctor.
    cli_authenticated: Callable[[str], bool | None] = _no_auth_probe
    # PATH lookup for the hook console script (injected so tests are
    # deterministic regardless of the runner's PATH).
    which: Callable[[str], str | None] = shutil.which
    # judge_llm model id -> None if LiteLLM resolves a provider, else a short
    # error string. Injected so the classifier check runs offline/deterministic.
    judge_model_resolvable: Callable[[str], str | None] = _real_judge_model_resolvable
    # issue_tracker config + env -> None if verify_auth succeeds, else a short
    # error string (FR-10.1). Injected so the tracker check runs offline.
    tracker_auth_probe: Callable[[object, Mapping[str, str]], str | None] = (
        _real_tracker_auth_probe
    )


def _real_cli_version(name: str) -> str | None:
    """Best-effort `<cli> --version`; None if the binary is not on PATH."""
    if shutil.which(name) is None:
        return None
    try:
        out = subprocess.run(
            [name, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr or "").strip()
    return text or None


def _real_cli_authenticated(name: str) -> bool | None:
    """Best-effort, non-interactive auth probe (FR-1.3).

    Runs the cheapest pin-verified, tool-less invocation each CLI supports and
    reads the exit code. A logged-out / unauthorized CLI exits non-zero (claude
    surfaces an in-band ``is_error`` result with exit 1; ``codex exec`` exits
    non-zero), which we treat as NOT authenticated so doctor fails closed
    *before* the first real agent step rather than after it. ``None`` means the
    probe could not run at all (binary absent, or it timed out / errored
    ambiguously); the caller then WARNs rather than asserting a state it could
    not observe.

    The invocations use exactly the flags ``.gauntlet/pins.yaml`` verified
    (claude: ``-p`` / ``--output-format json`` / ``--model haiku`` / ``--tools ""``;
    codex: ``exec --json -s read-only``). They make a minimal live model call;
    the P6 second-environment integration run confirms them end-to-end.
    """
    if shutil.which(name) is None:
        return None
    try:
        if name == "claude":
            proc = subprocess.run(
                ["claude", "-p", "--output-format", "json",
                 "--model", "haiku", "--tools", ""],
                input="ping", capture_output=True, text=True, timeout=90,
            )
        elif name == "codex":
            proc = subprocess.run(
                ["codex", "exec", "--json", "-s", "read-only", "-"],
                input="ping", capture_output=True, text=True, timeout=90,
            )
        else:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def real_probes() -> DoctorProbes:
    return DoctorProbes(
        cli_version=_real_cli_version,
        env=os.environ,
        cli_authenticated=_real_cli_authenticated,
        which=shutil.which,
        judge_model_resolvable=_real_judge_model_resolvable,
        tracker_auth_probe=_real_tracker_auth_probe,
    )


def _extract_version(text: str | None) -> str | None:
    """Pull a dotted version out of a `--version` line (`codex-cli 0.139.0`)."""
    if not text:
        return None
    m = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    return m.group(0) if m else text


def _pin_version(pins: PinFile | None, cli: str) -> str | None:
    if pins is None or cli not in pins.clis:
        return None
    return _extract_version(pins.clis[cli].version)


# ---- individual checks ------------------------------------------------------

def _check_version() -> CheckResult:
    return CheckResult("gauntlet", OK, f"version {__version__}")


def _check_cli(cli: str, probes: DoctorProbes, pins: PinFile | None) -> CheckResult:
    raw = probes.cli_version(cli)
    if raw is None:
        return CheckResult(
            cli, FAIL, "not found on PATH",
            remedy=f"install the {cli!r} CLI and authenticate it",
        )
    found = _extract_version(raw)
    pinned = _pin_version(pins, cli)
    detail = f"{raw}"
    if pinned and found and found != pinned:
        return CheckResult(
            cli, WARN,
            f"installed {found} != pin-verified {pinned}",
            remedy=(
                f"hook/flag behavior was verified against {cli} {pinned}; "
                "re-run the contract suite and update .gauntlet/pins.yaml if it "
                "still passes (FR-1.5)"
            ),
        )
    return CheckResult(cli, OK, detail + (f" (matches pin {pinned})" if pinned else ""))


def _check_cli_auth(cli: str, probes: DoctorProbes) -> CheckResult | None:
    """FR-1.3: a logged-out CLI must FAIL, not pass and break at the first step.

    Returns ``None`` (no row) when the CLI is absent — its FAIL is already owned
    by :func:`_check_cli`, so we do not double-report.
    """
    if probes.cli_version(cli) is None:
        return None
    name = f"{cli}-auth"
    authed = probes.cli_authenticated(cli)
    if authed is True:
        return CheckResult(name, OK, "authenticated")
    if authed is False:
        return CheckResult(
            name, FAIL, "not authenticated (or the CLI errored)",
            remedy=(
                f"log in to the {cli!r} CLI; a logged-out CLI passes a version "
                "check but fails at the first agent step (FR-1.3)"
            ),
        )
    return CheckResult(
        name, WARN, "could not verify authentication (no probe result)",
        remedy=f"confirm `{cli}` is logged in before starting a run",
    )


def _parse_json(path: Path) -> tuple[object | None, str | None]:
    """(value, error). error is a human string when the file is unreadable JSON."""
    try:
        return json.loads(path.read_text() or "null"), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _gauntlet_pretooluse_entries(settings: dict) -> list[dict]:
    """PreToolUse entries that route to the gauntlet judge hook.

    Matches on HOOK_COMMAND as a substring, not equality, so it recognises both
    the bare console script and the install-tolerant launcher that `gauntlet init`
    now wires (which calls the script via `command -v … && exec …`).
    """
    entries = []
    for entry in settings.get("hooks", {}).get("PreToolUse", []) or []:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook, dict) and HOOK_COMMAND in (hook.get("command") or ""):
                entries.append((entry, hook))
                break
    return entries


def _check_claude_hook(repo_root: Path, probes: DoctorProbes) -> CheckResult:
    """FR-1.3/FR-7.3: structurally verify the PreToolUse judge wiring (review F-005).

    Parse the file (a malformed settings.json is a FAIL, not a pass), verify a
    PreToolUse entry routes every tool (matcher ``*``) to the hook command as a
    ``command`` hook, and confirm the hook console script is actually on PATH.
    """
    path = repo_root / ".claude" / "settings.json"
    rel = ".claude/settings.json"
    if not path.exists():
        return CheckResult(
            "claude-hook", FAIL, f"{rel} missing",
            remedy="run `gauntlet init` to wire the PreToolUse judge hook",
        )
    settings, err = _parse_json(path)
    if err is not None or not isinstance(settings, dict):
        return CheckResult(
            "claude-hook", FAIL,
            f"{rel} is not a JSON object" + (f": {err}" if err else ""),
            remedy="fix the JSON; an unparseable settings file leaves the agent ungated",
        )
    matched = _gauntlet_pretooluse_entries(settings)
    if not matched:
        return CheckResult(
            "claude-hook", FAIL, f"{rel} has no PreToolUse entry wiring {HOOK_COMMAND}",
            remedy="run `gauntlet init` to add the PreToolUse judge hook",
        )
    # The judge must see every tool call; a narrower matcher silently leaves
    # tools ungated (FR-7.3 "100% blocked pre-execution").
    if not any(entry.get("matcher") == "*" for entry, _ in matched):
        scopes = ", ".join(sorted({str(e.get("matcher")) for e, _ in matched}))
        return CheckResult(
            "claude-hook", FAIL,
            f"{HOOK_COMMAND} wired only for matcher(s) {scopes}, not all tools (*)",
            remedy="set the PreToolUse matcher to `*` so every tool call is judged (FR-7.3)",
        )
    # The wired command must resolve to an executable, or the hook never runs.
    if probes.which(HOOK_COMMAND) is None:
        return CheckResult(
            "claude-hook", FAIL, f"{HOOK_COMMAND} wired but not found on PATH",
            remedy="reinstall gauntlet (`uv tool install` / `pipx install`) so the "
            "hook console script is on PATH (FR-1.1)",
        )
    _, hook = matched[0]
    if hook.get("type") != "command" or not hook.get("timeout"):
        return CheckResult(
            "claude-hook", WARN,
            f"PreToolUse → {HOOK_COMMAND} present but missing type=command/timeout",
            remedy="run `gauntlet init` to rewrite the canonical hook entry",
        )
    return CheckResult("claude-hook", OK, f"PreToolUse(*) → {HOOK_COMMAND}")


def _check_codex_hook(repo_root: Path, pins: PinFile | None) -> CheckResult:
    """Validate the codex hook config structurally (review F-005).

    Codex exec does not fire PreToolUse hooks on the pinned build (BOOTSTRAP-
    NOTES #10): its pre-execution control is the sandbox, so a missing or
    inert-but-wired config is not run-blocking — hence WARN, never FAIL. But we
    still parse it and verify it actually wires the hook command, rather than
    reporting OK for any file that merely exists.
    """
    path = repo_root / ".codex" / "hooks.json"
    rel = ".codex/hooks.json"
    if not path.exists():
        return CheckResult(
            "codex-hook", WARN, f"{rel} missing",
            remedy="run `gauntlet init` to write the (forward-looking) codex hooks config",
        )
    settings, err = _parse_json(path)
    if err is not None or not isinstance(settings, dict):
        return CheckResult(
            "codex-hook", WARN, f"{rel} is not a JSON object" + (f": {err}" if err else ""),
            remedy="run `gauntlet init` to rewrite the codex hooks config",
        )
    if not _gauntlet_pretooluse_entries(settings):
        return CheckResult(
            "codex-hook", WARN, f"{rel} does not wire {HOOK_COMMAND}",
            remedy="run `gauntlet init` to rewrite the codex hooks config",
        )
    # Sandbox-primary on the pinned build: a present-but-inert config is healthy
    # (BOOTSTRAP-NOTES #10). Report the firing status from the pin.
    note = f"PreToolUse → {HOOK_COMMAND}"
    if pins and "codex" in pins.clis:
        joined = " ".join(pins.clis["codex"].notes).lower()
        if "never fire" in joined or "does not fire" in joined:
            note += " (inert on pinned codex; sandbox-primary control — FR-7.3)"
    return CheckResult("codex-hook", OK, note)


def _has_symlinked_component(repo_root: Path, rel: str) -> bool:
    """True if ``rel`` or any component below ``repo_root`` is a symlink.

    ``Path.is_symlink()`` does not dereference, so this never follows a link out
    of the repository (review F-003). ``repo_root`` itself is excluded — a repo
    legitimately rooted on a symlinked path is not our concern, mirroring
    ``init._guard_regular_destination``.
    """
    current = repo_root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


# Each registry skill maps to a stable doctor check name. prd-author keeps its
# historical "prd-skill" name so existing diagnostics/tests are unchanged.
_SKILL_CHECK_NAMES = {
    "gauntlet-prd-author": "prd-skill",
    "gauntlet-operator": "operator-skill",
}


def _skill_check_name(spec) -> str:
    return _SKILL_CHECK_NAMES.get(spec.name, f"{spec.name}-skill")


def _playbook_check_name(spec) -> str:
    return f"{_skill_check_name(spec)}-playbook"


def _check_skill(repo_root: Path, spec, asset_root: str = ".") -> CheckResult:
    """FR-1.5 / FR-6.5: warn-only presence + format check for a registry skill.

    A committable skill gates nothing (it only routes a session to its playbook),
    so a problem here is **never** a FAIL — at worst a WARN. We surface a missing
    skill, a frontmatter block that does not parse against the normative schema
    (FR-1.5 / §6), or provenance that looks stale (a customized, provenance-bearing
    skill whose rendered playbook path no longer matches this repo's ``asset_root``
    — §4.5). A well-formed skill is OK. Malformed or unreadable skill state never
    hard-fails doctor (FR-1.5). Generalized over the skill registry so the operator
    skill is validated at the same severity as prd-author (FR-6.5, FR-7.1).
    """
    from gauntlet.engine import skill as S

    name = _skill_check_name(spec)
    rel = spec.skill_rel
    path = repo_root / rel
    remedy = f"run `gauntlet init` to (re)install the {spec.name} skill"
    # F-003: `Path.is_file()` / `read_text()` follow symlinks, so a skill path
    # pointed — directly or via a symlinked parent — at a file outside the repo
    # would be read and its contents surfaced in diagnostics. Refuse to
    # dereference; WARN without reading the target.
    if _has_symlinked_component(repo_root, rel):
        return CheckResult(
            name, WARN,
            f"{rel} is a symlink (or has a symlinked parent); not dereferenced",
            remedy=remedy,
        )
    if not path.exists():
        return CheckResult(
            name, WARN, f"{rel} missing ({spec.name} skill not installed)",
            remedy=remedy,
        )
    if not path.is_file():
        return CheckResult(
            name, WARN, f"{rel} is not a regular file", remedy=remedy,
        )
    # F-002: an unreadable or non-UTF-8 SKILL.md must not crash the warn-only
    # check (FR-1.5). UnicodeDecodeError is a ValueError; PermissionError an
    # OSError — both become a WARN, never an escaping exception.
    try:
        text = path.read_text()
    except (OSError, ValueError) as exc:
        return CheckResult(
            name, WARN, f"{rel} could not be read: {exc}", remedy=remedy,
        )
    violations = S.validate_skill_frontmatter(text)
    if violations:
        return CheckResult(
            name, WARN,
            f"{rel} frontmatter does not match the pinned schema: {violations[0]}",
            remedy=remedy,
        )
    # F-003: the schema only pins `name` to *some* kebab-case id, so an otherwise
    # valid SKILL.md whose frontmatter names a *different* skill would pass schema
    # validation and (with its playbook ref intact) classify as a customization —
    # leaving doctor reporting OK while the skill's discovery surface is broken.
    # The installed file at this spec's path must declare this spec's name.
    meta = S.parse_frontmatter(text)
    declared = meta.get("name") if meta else None
    if declared != spec.name:
        return CheckResult(
            name, WARN,
            f"{rel} frontmatter name {declared!r} does not match expected "
            f"{spec.name!r}",
            remedy=remedy,
        )
    if (
        spec.classify(text) == "customization"
        and spec.looks_stale(text, asset_root)
    ):
        return CheckResult(
            name, WARN,
            f"{rel} provenance looks stale: playbook ref "
            f"{spec.playbook_ref(asset_root)!r} not found (asset_root may have changed)",
            remedy="re-render the skill (`gauntlet init`) or update its playbook "
            "reference; init never modifies a customized skill",
        )
    return CheckResult(name, OK, f"{rel} present and well-formed")


def _check_playbook(repo_root: Path, spec, asset_root: str = ".") -> CheckResult:
    """FR-6.5: warn-only presence check for a skill's playbook (the file it points
    at). A skill that resolves to an absent playbook routes a session to nothing,
    so surface it — but, like the skill itself, it gates no run, so it is never a
    FAIL. Symlinks are not dereferenced (F-003)."""
    name = _playbook_check_name(spec)
    rel = spec.playbook_ref(asset_root)
    path = repo_root / rel
    remedy = f"run `gauntlet init` to (re)install the {spec.name} playbook"
    if _has_symlinked_component(repo_root, rel):
        return CheckResult(
            name, WARN,
            f"{rel} is a symlink (or has a symlinked parent); not dereferenced",
            remedy=remedy,
        )
    if not path.exists():
        return CheckResult(
            name, WARN, f"{rel} missing ({spec.name} playbook the skill points at)",
            remedy=remedy,
        )
    if not path.is_file():
        return CheckResult(name, WARN, f"{rel} is not a regular file", remedy=remedy)
    return CheckResult(name, OK, f"{rel} present")


def _check_judge(repo_root: Path, asset_root: str = ".") -> CheckResult:
    policy = repo_root / asset_root / "policy.yaml"
    if not policy.exists():
        return CheckResult(
            "judge", FAIL, "policy.yaml missing",
            remedy="run `gauntlet init` to scaffold the judge fast-path policy",
        )
    try:
        from gauntlet.judge.policy import Policy

        Policy.load(policy)
    except Exception as exc:  # parse / schema fault
        return CheckResult(
            "judge", FAIL, f"policy.yaml does not load: {exc}",
            remedy="fix the YAML / regex in policy.yaml (FR-7.6)",
        )
    return CheckResult("judge", OK, "policy.yaml loads; judge startable")


def _check_judge_classifier(config, probes: DoctorProbes) -> CheckResult:
    """FR-7.2: the judge's LLM classifier rung. With no resolvable ``judge_llm``
    model, every command the policy fast-path does not match (and every ``ask``
    rule) fails closed — a silent footgun surfaced HERE, before a run, rather
    than left for the operator to infer from a later wall of deny errors. (The
    key itself is covered by ``api-keys``; this check is about whether a
    classifier is configured, uses the ``api`` adapter the engine actually runs
    it as, and has a resolvable model id.)"""
    profile = config.agents.get("judge_llm")
    model = getattr(profile, "model", None) if profile is not None else None
    if not model:
        return CheckResult(
            "judge-classifier", WARN,
            "no `judge_llm` profile: LLM classifier disabled — commands the "
            "policy.yaml fast-path does not match will fail closed",
            remedy="add a `judge_llm` agent profile (e.g. api / gpt-5-mini) to "
            "enable classification, or accept fail-closed-only operation",
        )
    # The engine's _with_judge() ALWAYS builds an ApiAdapter from judge_llm.model
    # (the classifier is a non-agentic LiteLLM call by design), ignoring the
    # configured adapter — and _check_api_keys() only validates keys for `api`
    # profiles. So a non-`api` judge_llm would pass doctor yet fail closed at
    # runtime with no key checked (PR #13 review). FAIL: enforce the config
    # contract matches how the classifier is actually run.
    adapter = getattr(profile, "adapter", None)
    if adapter != "api":
        return CheckResult(
            "judge-classifier", FAIL,
            f"judge_llm uses adapter {adapter!r}, but the engine always runs the "
            "classifier as an `api` (LiteLLM) call — the configured adapter is "
            "ignored and its key is not validated",
            remedy="set judge_llm.adapter: api so doctor checks the key and the "
            "config matches how the classifier actually runs",
        )
    err = probes.judge_model_resolvable(model)
    if err:
        return CheckResult(
            "judge-classifier", WARN,
            f"judge_llm model {model!r} is not resolvable by LiteLLM: {err}",
            remedy="use a valid LiteLLM model id (e.g. `gpt-5-mini` or "
            "`anthropic/claude-haiku-4-5`); an unresolvable model fails every "
            "classifier call closed",
        )
    return CheckResult(
        "judge-classifier", OK, f"LLM classifier model {model!r} resolvable"
    )


def _check_tracker(config, probes: DoctorProbes) -> CheckResult | None:
    """FR-10.1: validate the `gauntlet review` issue-tracker when configured.

    Only emitted when an ``issue_tracker`` block is present (and not disabled via
    ``provider: none``). Checks provider is supported (config load already
    enforces this), the named auth env var is set, and a cheap ``verify_auth``
    probe succeeds — each failing closed with an actionable remedy."""
    tracker = getattr(config, "issue_tracker", None)
    if tracker is None or not tracker.enabled:
        return None
    env_name = tracker.api_key_env
    if not probes.env.get(env_name):
        return CheckResult(
            "issue-tracker", FAIL,
            f"issue_tracker provider {tracker.provider!r} configured but "
            f"{env_name} is not set",
            remedy=f"export {env_name} with a {tracker.provider} API token "
            "(the token lives in the environment, never in repo config)",
        )
    err = probes.tracker_auth_probe(tracker, probes.env)
    if err:
        return CheckResult(
            "issue-tracker", FAIL,
            f"issue_tracker {tracker.provider!r} auth probe failed: {err}",
            remedy=f"check that {env_name} holds a valid {tracker.provider} token "
            "and the API is reachable (a review with --issue will fail closed "
            "otherwise)",
        )
    return CheckResult(
        "issue-tracker", OK,
        f"{tracker.provider} authenticated (env {env_name})",
    )


def _check_test_command(config) -> CheckResult:
    """Issue #18: a config left with the un-configured placeholder would fail
    every phase's test gate. Surface it as a WARN here, before a run, rather than
    leaving the operator to decode a failed shell step mid-pipeline."""
    from gauntlet.engine.detect import is_placeholder_command

    command = getattr(config, "test_command", None)
    remedy = (
        "set test_command in .gauntlet/config.yaml to the command that runs this "
        "project's tests (must exit non-zero on failure)"
    )
    # An empty / whitespace-only command runs nothing under shell=True yet exits 0,
    # so the test gate would silently pass — a fail-open. WARN it like the
    # placeholder rather than reporting OK.
    if not command or not str(command).strip():
        return CheckResult(
            "test-command", WARN,
            "test_command is empty — the test gate would run nothing and pass",
            remedy=remedy,
        )
    if is_placeholder_command(command):
        return CheckResult(
            "test-command", WARN,
            "test_command is the un-configured placeholder (gauntlet init could "
            "not auto-detect one)",
            remedy=remedy,
        )
    return CheckResult("test-command", OK, f"test_command: {command!r}")


def _required_key(model: str) -> str | None:
    low = model.lower()
    for prefix, var in _KEY_BY_PREFIX.items():
        if low.startswith(prefix):
            return var
    return None


# Step keys that reference a named agent profile (FR-2.1). `agents` (list) is the
# retrospective step's form.
_AGENT_REF_KEYS = ("agent", "reviewer", "triager", "fixer", "escalation_agent",
                   "message_agent")


def _referenced_agents(repo_root: Path, asset_root: str = ".") -> set[str] | None:
    """Agent profile names the default pipeline references, or None if it cannot
    be loaded (the caller then treats every configured api profile as required —
    fail closed)."""
    pipeline_path = repo_root / asset_root / "pipelines" / "standard.yaml"
    if not pipeline_path.exists():
        return None
    try:
        from gauntlet.engine.pipeline import load_pipeline

        pipeline, _ = load_pipeline(pipeline_path)
    except Exception:
        return None
    names: set[str] = set()
    for step in pipeline.all_steps():
        for key in _AGENT_REF_KEYS:
            val = step.get(key)
            if isinstance(val, str):
                names.add(val)
        agents = step.get("agents")
        if isinstance(agents, list):
            names.update(a for a in agents if isinstance(a, str))
    return names


def _check_api_keys(
    config, probes: DoctorProbes, referenced: set[str] | None
) -> CheckResult:
    """FR-1.3/FR-1.4: every api profile the run needs must have its key in the
    environment. A referenced profile missing its credential is a FAIL — not a
    WARN that lets doctor exit zero for an unusable pipeline (review F-006)."""
    api_profiles = {
        name: p.model
        for name, p in config.agents.items()
        if p.adapter == "api" and p.model
    }
    if not api_profiles:
        return CheckResult("api-keys", WARN, "no `api` adapter profiles configured")

    # referenced=None means we could not resolve the pipeline; require every
    # configured profile (fail closed).
    def required_profile(name: str) -> bool:
        return referenced is None or name in referenced

    required: dict[str, set[str]] = {}        # env var -> referenced profiles
    optional_missing: dict[str, set[str]] = {}  # env var -> unused profiles
    unknown: list[str] = []
    for name, model in api_profiles.items():
        var = _required_key(model)
        if var is None:
            unknown.append(f"{name}={model}")
            continue
        if required_profile(name):
            required.setdefault(var, set()).add(name)
        elif not probes.env.get(var):
            optional_missing.setdefault(var, set()).add(name)

    have = sorted(v for v in required if probes.env.get(v))
    missing = sorted(set(required) - set(have))
    if missing:
        needed = ", ".join(sorted(n for v in missing for n in required[v]))
        return CheckResult(
            "api-keys", FAIL,
            f"missing {', '.join(missing)} required by profile(s): {needed}",
            remedy="export the key(s) in your shell / keychain, never in repo config (FR-1.4)",
        )
    detail = f"present: {', '.join(have)}" if have else "none required"
    warn_bits: list[str] = []
    if optional_missing:
        om = ", ".join(
            f"{v} ({', '.join(sorted(optional_missing[v]))})"
            for v in sorted(optional_missing)
        )
        warn_bits.append(f"unused profile(s) missing {om}")
    if unknown:
        warn_bits.append(f"could not infer the key var for: {', '.join(unknown)}")
    if warn_bits:
        return CheckResult("api-keys", WARN, f"{detail}; " + "; ".join(warn_bits))
    return CheckResult("api-keys", OK, detail)


def _check_repo_secrets(repo_root: Path, asset_root: str = ".") -> CheckResult:
    """FR-1.4: no credential literal may live in committed repo config."""
    offenders: list[str] = []
    for rel in (".gauntlet/config.yaml",
                (Path(asset_root) / "policy.yaml").as_posix(),
                (Path(asset_root) / "pipelines" / "standard.yaml").as_posix()):
        path = repo_root / rel
        if not path.exists():
            continue
        text = path.read_text()
        if any(p.search(text) for p in _SECRET_PATTERNS):
            offenders.append(rel)
    if offenders:
        return CheckResult(
            "repo-secrets", FAIL,
            f"credential-shaped literal found in {', '.join(offenders)}",
            remedy="remove the secret; keys live in env/keychain only (FR-1.4)",
        )
    return CheckResult("repo-secrets", OK, "no credential literals in repo config")


def _check_pin_file(repo_root: Path, pins: PinFile | None) -> CheckResult:
    if pins is None:
        return CheckResult(
            "pin-file", WARN, f"{pin_file_path(repo_root)} missing or unreadable",
            remedy="run the contract suite to regenerate .gauntlet/pins.yaml (FR-1.5)",
        )
    return CheckResult(
        "pin-file", OK,
        f"verified {pins.verified_date}; clis: {', '.join(sorted(pins.clis))}",
    )


# ---- aggregation ------------------------------------------------------------

def run_doctor(
    repo_root: Path, *, probes: DoctorProbes | None = None
) -> list[CheckResult]:
    """Run every environment check; returns results in display order."""
    probes = probes or real_probes()

    try:
        pins = load_pins(pin_file_path(repo_root))
    except Exception:
        pins = None

    results = [_check_version()]
    for cli in AGENT_CLIS:
        results.append(_check_cli(cli, probes, pins))
        auth = _check_cli_auth(cli, probes)
        if auth is not None:
            results.append(auth)
    # Load the run config once up front: its `asset_root` tells the judge /
    # secrets / pipeline checks where assets live (default "." = repo root). A
    # missing/unloadable config is itself surfaced as a FAIL below.
    try:
        from gauntlet.engine.config import RunConfig

        config = RunConfig.load(repo_root / ".gauntlet" / "config.yaml")
        config_error: Exception | None = None
    except Exception as exc:
        config, config_error = None, exc
    asset_root = config.asset_root if config is not None else "."

    results.append(_check_claude_hook(repo_root, probes))
    results.append(_check_codex_hook(repo_root, pins))
    # Validate every committable skill (prd-author + operator) and its playbook at
    # the same warn-only severity (FR-6.5, FR-7.1). Imported here to avoid a
    # module-load cycle and to keep doctor's import surface lazy.
    from gauntlet.engine import skill as S

    for spec in S.SKILL_REGISTRY:
        results.append(_check_skill(repo_root, spec, asset_root))
        results.append(_check_playbook(repo_root, spec, asset_root))
    results.append(_check_judge(repo_root, asset_root))
    results.append(_check_repo_secrets(repo_root, asset_root))

    if config is None:
        results.append(CheckResult(
            "config", FAIL, f".gauntlet/config.yaml not loadable: {config_error}",
            remedy="run `gauntlet init` to scaffold a config",
        ))
    else:
        referenced = _referenced_agents(repo_root, asset_root)
        # The engine-managed judge always consumes the `judge_llm` profile when
        # configured, even though no pipeline step names it (FR-7.1) — count it
        # as referenced so its key is required, not treated as unused.
        if referenced is not None and "judge_llm" in config.agents:
            referenced = referenced | {"judge_llm"}
        results.append(_check_api_keys(config, probes, referenced))
        results.append(_check_judge_classifier(config, probes))
        results.append(_check_test_command(config))
        tracker_check = _check_tracker(config, probes)
        if tracker_check is not None:
            results.append(tracker_check)
    results.append(_check_pin_file(repo_root, pins))
    return results


def has_failure(results: list[CheckResult]) -> bool:
    return any(r.status == FAIL for r in results)
